import random
from collections import defaultdict, deque, namedtuple

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as f

from algo.algo import Algorithm
from algo.epsilon import EpsilonLinearDecay
from envs.env import Observation, Player

Transition = namedtuple(
    "Transition",
    ("state", "action", "valid_actions", "next_state", "reward", "terminate"),
)

DQN_STR = "DQN"


def init_prev():
    return None


class Memory:
    def __init__(self, max_size: int) -> None:
        self.max_size = max_size
        self.memory = deque([], maxlen=max_size)

    def push(self, transition: Transition):
        self.memory.append(transition)

    def sample(self, batch_size: int):
        return random.sample(self.memory, batch_size)

    def __len__(self):
        return len(self.memory)


class MLP(nn.Module):
    def __init__(self, input_size: int, output_size: int, hidden_layers: list):
        super(MLP, self).__init__()
        layers = list()
        layers.append(nn.Linear(input_size, hidden_layers[0]))
        layers.append(nn.ReLU())
        for i in range(1, len(hidden_layers)):
            layers.append(nn.Linear(hidden_layers[i - 1], hidden_layers[i]))
            layers.append(nn.ReLU())
        layers.append(nn.Linear(hidden_layers[-1], output_size))
        self.model = nn.Sequential(*layers)

    def forward(self, x):
        return self.model(x)


class DQN(Algorithm):
    def __init__(
        self,
        state_size: int,
        num_actions: int,
        hidden_layers: list,
        approx_steps: int,  # for eps decay
        player: Player = Player(0),
        memory_size: int = 20_000,
        batch_size: int = 256,
        eps_init: float = 1.0,
        eps_final: float = 0.1,
        learning_rate: float = 0.02,  # alpha
        gamma: float = 0.99,  # discount
        target_update_every: int = 1000,  # target network update per this step
        optimize_every: int = 16,
        soft: bool = False,
        tau: float = 0.005,  # soft update rate
    ) -> None:
        super().__init__()
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.num_actions = num_actions
        self.hidden_layers = hidden_layers
        self.q_net = MLP(state_size, num_actions, hidden_layers).to(self.device)
        self.target_net = MLP(state_size, num_actions, hidden_layers).to(self.device)
        self.target_net.eval()
        self.target_net.load_state_dict(self.q_net.state_dict())
        self.optimizer = torch.optim.Adam(self.q_net.parameters(), lr=learning_rate)
        self.criterion = f.smooth_l1_loss
        self.player = player
        self.memory = Memory(memory_size)
        self.batch_size = batch_size
        self.eps_schedule = EpsilonLinearDecay(
            eps_init, eps_final, approx_steps + self.memory.max_size
        )
        self.epsilon = self.eps_schedule.value
        self.learning_rate = learning_rate
        self.gamma = gamma
        self.target_update_every = target_update_every
        self.optimize_every = optimize_every
        self.tau = tau
        self.soft = soft
        self._prev_state = defaultdict(init_prev)
        self._prev_action = defaultdict(init_prev)

    def step(
        self,
        obs: Observation,
        eval: bool = False,
        self_play: bool = False,
        eps: float = 0.0,
    ):

        if not obs.terminate:
            action = self._act_e_greedy(obs.state, obs.valid_actions, eval, eps)
        else:
            action = None

        if not eval:

            if self.eps_schedule.steps_taken % self.optimize_every == 0:
                self._optimize(self_play)

            if self.eps_schedule.steps_taken % self.target_update_every == 0:
                self._update_target(self.soft)

            if self._prev_state[self.player]:
                transition = Transition(
                    self._prev_state[self.player],  # tuple
                    self._prev_action[self.player],  # int
                    obs.valid_actions,  # list
                    obs.state,  # tuple
                    obs.reward[self.player],  # int
                    obs.terminate,  # bool
                )
                self.memory.push(transition)

            if obs.terminate:
                self._prev_state[self.player] = None
                self._prev_action[self.player] = None
                return
            else:
                self._prev_state[self.player] = obs.state
                self._prev_action[self.player] = action

        return action

    def _optimize(self, self_play: bool):
        if len(self.memory) < self.memory.max_size:
            return

        transition_batch = self.memory.sample(self.batch_size)
        state_batch = torch.stack(
            [
                torch.tensor(t.state, dtype=torch.float32, device=self.device)
                for t in transition_batch
            ]
        )
        action_batch = torch.tensor(
            [obs.action for obs in transition_batch],
            dtype=torch.int64,
            device=self.device,
        ).unsqueeze(1)

        valid_action_batch = []
        for t in transition_batch:
            valid_actions_mask = np.zeros(self.num_actions)
            valid_actions_mask[t.valid_actions] = 1.0
            valid_action_batch.append(
                torch.tensor(
                    valid_actions_mask, dtype=torch.float32, device=self.device
                )
            )
        valid_action_batch = torch.stack(valid_action_batch)

        next_state_batch = torch.stack(
            [
                torch.tensor(t.next_state, dtype=torch.float32, device=self.device)
                for t in transition_batch
            ]
        )

        reward_batch = torch.tensor(
            [t.reward for t in transition_batch],
            dtype=torch.float32,
            device=self.device,
        ).unsqueeze(1)
        done_batch = torch.tensor(
            [t.terminate for t in transition_batch], device=self.device
        ).unsqueeze(1)

        q_values = self.q_net(state_batch)
        target_q_values = self.target_net(next_state_batch).detach()
        invalid_actions_batch = 1 - valid_action_batch
        valid_target_q_values = target_q_values.masked_fill(
            invalid_actions_batch.bool(), -np.finfo(np.float32).max
        )
        max_next_q = torch.max(valid_target_q_values, dim=1)[0]
        if self_play:
            reward_batch[reward_batch == -1] = 1  # self play, removable
        target = (
            reward_batch
            + done_batch.logical_not() * self.gamma * max_next_q.unsqueeze(1)
        )
        pred = q_values.gather(1, action_batch)

        loss = self.criterion(pred, target)
        self.optimizer.zero_grad()
        loss.backward()
        # torch.nn.utils.clip_grad_norm_(self.q_net.parameters(), max_norm=1.0) # needed?
        self.optimizer.step()

    def _act_e_greedy(
        self, state: tuple, valid_actions: list, eval: bool, eps: float = 0.0
    ):
        epsilon = eps if eval else self.epsilon
        if np.random.rand() < 1 - epsilon:
            action = self._act_greedy(state, valid_actions)
        else:
            action = self._act_random(valid_actions)
        self.epsilon = self.eps_schedule.step()
        return action

    def _act_greedy(self, state: tuple, valid_actions: list):
        state = torch.tensor(state, device=self.device, dtype=torch.float32)
        q_values = self.q_net(state).detach()
        legal_action_mask = torch.full(
            q_values.shape, float("-inf"), device=self.device
        )
        legal_action_mask[valid_actions] = q_values[valid_actions]
        action = torch.argmax(legal_action_mask).item()
        return action

    def _act_random(self, valid_actions: list):
        return random.choice(valid_actions)

    def _update_target(self, soft: bool):
        if soft:
            q_state_dict = self.q_net.state_dict()
            t_state_dict = self.target_net.state_dict()
            for key in q_state_dict:
                t_state_dict[key] = q_state_dict[key] * self.tau + t_state_dict[key] * (
                    1 - self.tau
                )
            self.target_net.load_state_dict(t_state_dict)
        else:
            self.target_net.load_state_dict(self.q_net.state_dict())

    def save(self, fname: str):
        torch.save(self.q_net.state_dict(), f"{str(self)}_{fname}.pt")

    def load(self, fname: str, eval: bool):
        weights = torch.load(fname)
        self.q_net.load_state_dict(weights)
        self.target_net.load_state_dict(weights)
        if eval:
            self.q_net.eval()
        self.target_net.eval()

    def __str__(self):
        return DQN_STR
