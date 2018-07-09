from collections import defaultdict

import torch

from ..data import Row
from .modules import Hidden2Normal, InputEmbedding


class OLSTM(torch.nn.Module):
    def __init__(self, embedding_dim=64, hidden_dim=128, directional=False):
        super(OLSTM, self).__init__()
        self.hidden_dim = hidden_dim
        self.embedding_dim = embedding_dim
        self.grid_fn = occupancy if not directional else directional_occupancy

        self.input_embeddings = InputEmbedding(self.embedding_dim, 4.0)
        self.lstm = torch.nn.LSTMCell(self.embedding_dim + 36, self.hidden_dim)

        # Predict the parameters of a multivariate normal:
        # mu_vel_x, mu_vel_y, sigma_vel_x, sigma_vel_y, rho
        self.hidden2normal = Hidden2Normal(self.hidden_dim)

    def forward(self, observed, other_paths):
        """forward

        observed shape is (seq, batch, observables)
        """
        batch_size = observed.shape[1]
        hidden_cell_state = (torch.zeros(batch_size, self.hidden_dim),
                             torch.zeros(batch_size, self.hidden_dim))
        # hidden_cell_state = self.lstm(self.input_embeddings.start_tag(), hidden_cell_state)

        normals = []
        positions = []
        for obs1, obs2, others_obs1, others_obs2 in zip(
                observed[:-1], observed[1:],
                other_paths[:-1], other_paths[1:]):
            emb = torch.cat([
                self.input_embeddings(obs2 - obs1),
                self.grid_fn(obs1, obs2, others_obs1, others_obs2),
            ], dim=1)
            hidden_cell_state = self.lstm(emb, hidden_cell_state)

            normal = self.hidden2normal(hidden_cell_state[0])
            normals.append(normal)
            positions.append(obs2 + normal[:, :2])

        # end one coordinate before the end so that it won't predict past the
        # last coordinate
        for others_obs1, others_obs2 in zip(other_paths[len(observed) - 1:-2],
                                            other_paths[len(observed):-1]):
            input_vel = (positions[-1] - positions[-2]).detach()  # DETACH!!!
            emb = torch.cat([
                self.input_embeddings(input_vel),
                self.grid_fn(positions[-2], positions[-1], others_obs1, others_obs2),
            ], dim=1)
            hidden_cell_state = self.lstm(emb, hidden_cell_state)

            normal = self.hidden2normal(hidden_cell_state[0])
            normals.append(normal)
            positions.append(positions[-1] + normal[:, :2])

        return torch.stack(normals if self.training else positions, dim=0)


def occupancy(_, xy2, __, other_xy2, cell_side=0.5, n=6):
    """Returns the occupancy."""

    xy = xy2[0]
    oxy = torch.Tensor([o for o in other_xy2 if o[0] is not None])
    if not oxy.shape[0]:
        return torch.zeros(1, n * n)

    oij = ((oxy - xy) / cell_side + n / 2)
    range_violations = torch.sum((oij < 0) + (oij >= n), dim=1)
    oij = oij[range_violations == 0, :].long()
    if oij.shape[0] == 0:
        return torch.zeros(1, n * n)
    oi = (oij[:, 0] * n + oij[:, 1])
    occ = torch.zeros(1, n * n)
    occ[:, oi] = 1

    return occ


def directional_occupancy(xy1, xy2, other_xy1, other_xy2, cell_side=0.5, nx=6, ny=6):
    """Returns the occupancy."""
    xy1 = xy1[0]
    xy2 = xy2[0]
    ref_direction = torch.atan2(xy2[1] - xy1[1], xy2[0] - xy1[0])

    def is_occupied_with_direction(x_min, y_min, x_max, y_max):
        for (x1, y1), (x2, y2) in zip(other_xy1, other_xy2):
            if x1 is None or y1 is None or x2 is None or y2 is None:
                continue
            if x_min < x2 and x2 < x_max and y_min < y2 and y2 < y_max:
                direction = ref_direction - torch.atan2(y2 - y1, x2 - x1)
                forward = -torch.pi/2 < direction < torch.pi < 2
                if forward:
                    return (1, 0)
                return (1, 1)

        return (0, 0)

    x, y = xy2
    grid_x = torch.linspace(-nx/2 * cell_side, nx/2 * cell_side, nx + 1) + x
    grid_y = torch.linspace(-ny/2 * cell_side, ny/2 * cell_side, ny + 1) + y
    return torch.Tensor([[
        v
        for xx1, xx2 in zip(grid_x[:-1], grid_x[1:])
        for yy1, yy2 in zip(grid_y[:-1], grid_y[1:])
        for v in is_occupied_with_direction(xx1, yy1, xx2, yy2)
    ]])


class OLSTMPredictor(object):
    def __init__(self, model, model_vanilla):
        self.model = model
        self.model_vanilla = model_vanilla

    def save(self, filename):
        with open(filename, 'wb') as f:
            torch.save(self, f)

    @staticmethod
    def load(filename):
        with open(filename, 'rb') as f:
            return torch.load(f)

    def others_xy(self, paths, n_predict=12):
        self.model_vanilla.eval()

        others_xy = defaultdict(dict)
        frames = [r.frame for r in paths[0][:9]]
        frames_set = set(frames)
        pedestrians = set()
        pedestrians_to_predict = set()
        for path in paths[1:]:
            for row in path:
                if row.frame not in frames_set:
                    continue
                others_xy[row.pedestrian][row.frame] = (row.x, row.y)
                pedestrians.add(row.pedestrian)
                if row.frame == frames[-1]:
                    pedestrians_to_predict.add(row.pedestrian)

        predicted = {}
        for ped_id in pedestrians_to_predict:
            xy = [[xy] for _, xy in sorted(others_xy[ped_id].items())]
            if len(xy) < 4:
                continue
            observed = torch.Tensor(xy)
            with torch.no_grad():
                output = self.model_vanilla(observed, n_predict=n_predict)
                output = output[-n_predict:].numpy()
            predicted[ped_id] = [xy for (xy,) in output]

        # preserve order
        pedestrians = list(pedestrians)

        # observed part
        obs_result = [[others_xy.get(ped_id, {}).get(frame, (None, None))
                       for ped_id in pedestrians]
                      for frame in frames]
        # predicted part
        pred_result = [[predicted[ped_id][i] if ped_id in predicted else (None, None)
                        for ped_id in pedestrians]
                       for i in range(n_predict)]
        return obs_result + pred_result

    def __call__(self, paths, n_predict=12):
        self.model.eval()

        observed_path = paths[0]
        ped_id = observed_path[0].pedestrian
        with torch.no_grad():
            observed = torch.Tensor([[(r.x, r.y)] for r in observed_path[:9]])

            others_xy = self.others_xy(paths, n_predict)
            outputs = self.model(observed, others_xy)[-n_predict:]

        return [Row(0, ped_id, x, y) for ((x, y),) in outputs]