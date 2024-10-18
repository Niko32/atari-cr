from typing import Optional, Union, Callable, Tuple, List
from itertools import product
import os
import time

import numpy as np
import torch
from torch.optim import Adam
import torch.nn.functional as F
from torchvision.transforms import Resize

from ray import train
from gymnasium.vector import VectorEnv
from gymnasium.spaces import Discrete

from active_gym import FixedFovealEnv
from atari_cr.atari_head.dataset import GazeDataset
from atari_cr.atari_head.gaze_predictor import GazePredictor
from atari_cr.pauseable_env import PauseableFixedFovealEnv
from atari_cr.models import EpisodeInfo
from atari_cr.buffers import DoubleActionReplayBuffer
from atari_cr.pvm_buffer import PVMBuffer
from atari_cr.utils import linear_schedule
from atari_cr.agents.dqn_atari_cr.networks import QNetwork, SelfPredictionNetwork

class CRDQN:
    """
    Algorithm for DQN with Computational Rationality
    """
    def __init__(
            self,
            env: Union[PauseableFixedFovealEnv, FixedFovealEnv],
            eval_env_generator: Callable[[int], Union[
                VectorEnv, FixedFovealEnv, PauseableFixedFovealEnv]],
            sugarl_r_scale: float,
            seed = 0,
            fov_size = 50,
            sensory_action_space_quantization: Tuple[int] = (4, 4),
            learning_rate = 0.0001,
            replay_buffer_size = 100000,
            frame_stack = 4,
            pvm_stack = 3,
            epsilon_interval: Tuple[float] = (1., 0.01),
            exploration_fraction = 0.10,
            batch_size = 32,
            learning_start = 80000,
            train_frequency = 4,
            target_network_frequency = 1000,
            eval_frequency = -1,
            gamma = 0.99,
            cuda = True,
            n_evals = 10,
            ignore_sugarl = True,
            no_model_output = False,
            no_pvm_visualization = False,
            capture_video = True,
            agent_id = 0,
            debug = False,
            score_target = True,
            evaluator: Optional[GazePredictor] = None,
        ):
        """
        :param env `gymnasium.Env`:
        :param Callable eval_env_generator: Function, outputting an eval env given a
            seed
        :param float sugarl_r_scale:
        :param int seed:
        :param int fov_size:
        :param Tuple[int] sensory_action_space_quantization: The number of smallest
            sensory steps it takes from left to right and from top to bottom
        :param float learning_rate: The learning rate used for the Q Network and Self
            Predicition Network
        "param int replay_buffer_size:
        :param int frame_stack: The number of frames being stacked as on observation by
            the atari environment
        :param int pvm_stack: The number of recent observations to be used for action
            selection
        :param Tuple[int] epsilon_interval: Interval in which the propability for a
            random action epsilon moves from one end to the other during training
        :param float exploration_fraction: The fraction of the total learning time steps
            it takes for epsilon to reach its end value
        :param int batch_size:
        :param int learning_start: The timestep at which to start training the Q Network
        :param int train_frequency: The number of timesteps between training sessions
        :param int target_network_frequency: The number of timesteps between target
            network updates
        :param int eval_frequency: The number of timesteps between evaluations; -1 for
            eval at the end
        :param float gamma: The discount factor gamma
        :param bool cuda: Whether to use cuda or not
        :param int n_evals: Number of eval episodes to be played
        :param bool ignore_sugarl: Whether to ignore the sugarl term in the loss
            calculation
        :param int agent_id: Identifier for an agent when used together with other
            agents
        :param GazePredictor evaluator: Supervised learning model trained on human data.
            Reference for how human plausible the models' gazes are
        """
        self.env = env
        self.sugarl_r_scale = sugarl_r_scale
        self.seed = seed
        self.fov_size = fov_size
        self.epsilon_interval = epsilon_interval
        self.exploration_fraction = exploration_fraction
        self.batch_size = batch_size
        self.learning_start = learning_start
        self.gamma = gamma
        self.train_frequency = train_frequency
        self.target_network_frequency = target_network_frequency
        self.eval_frequency = eval_frequency
        self.n_evals = n_evals
        self.eval_env_generator = eval_env_generator
        self.pvm_stack = pvm_stack
        self.frame_stack = frame_stack
        self.ignore_sugarl = ignore_sugarl
        self.no_model_output = no_model_output
        self.no_pvm_visualization = no_pvm_visualization
        self.capture_video = capture_video
        self.agent_id = agent_id
        self.debug = debug
        self.score_target = score_target
        self.evaluator = evaluator

        self.n_envs = len(self.env.envs) if isinstance(self.env, VectorEnv) else 1
        self.current_timestep = 0

        # Get the observation size
        self.envs = env.envs if isinstance(env, VectorEnv) else [env]
        for env in self.envs:
            assert isinstance(env, (PauseableFixedFovealEnv, FixedFovealEnv)), \
                "The environment is expected to be wrapped in a PauseableFixedFovealEnv"
        self.obs_size = self.env.observation_space.shape[2:]
        assert len(self.obs_size) == 2, "The CRDQN agent only supports 2D Environments"

        # Get the sensory action set as a list of discrete actions
        # How far can the fovea move from left to right and from top to bottom
        max_sensory_action_step = np.array(self.obs_size) - np.array(
            [self.fov_size, self.fov_size])
        sensory_action_step_size = max_sensory_action_step // \
            sensory_action_space_quantization
        sensory_action_x_set = list(range(0, max_sensory_action_step[0],
            sensory_action_step_size[0]))[:sensory_action_space_quantization[0]]
        sensory_action_y_set = list(range(0, max_sensory_action_step[1],
            sensory_action_step_size[1]))[:sensory_action_space_quantization[1]]
        # Discrete action set as cross product of possible x and y steps
        self.sensory_action_set = [np.array(a)
            for a in list(product(sensory_action_x_set, sensory_action_y_set))]

        self.device = torch.device(
            "cuda" if torch.cuda.is_available() and cuda else "cpu")
        assert self.device.type == "cuda", \
            f"Set up cuda to run. Current device: {self.device.type}"

        # Q networks
        self.q_network = QNetwork(self.env, self.sensory_action_set).to(self.device)
        self.optimizer = Adam(self.q_network.parameters(), lr=learning_rate)
        self.target_network = QNetwork(
            self.env, self.sensory_action_set).to(self.device)
        self.target_network.load_state_dict(self.q_network.state_dict())

        # Self Prediction Networks; used to judge the quality of sensory actions
        self.sfn = SelfPredictionNetwork(self.env).to(self.device)
        self.sfn_optimizer = Adam(self.sfn.parameters(), lr=learning_rate)

        # Replay Buffer aka. Long Term Memory
        self.rb = DoubleActionReplayBuffer(
            replay_buffer_size,
            self.env.single_observation_space,
            self.env.single_action_space["motor_action"],
            Discrete(len(self.sensory_action_set)),
            self.device,
            n_envs=self.env.num_envs if isinstance(self.env, VectorEnv) else 1,
            optimize_memory_usage=True,
            handle_timeout_termination=False,
        )

        # PVM Buffer aka. Short Term Memory, combining multiple observations
        self.pvm_buffer = PVMBuffer(
            pvm_stack, (self.n_envs, frame_stack, *self.obs_size))

        self.auc = 0.

    def learn(self, n: int, env_name: str, experiment_name: str):
        """
        Acts in the environment and trains the agent for n timesteps
        """
        # Define output paths
        run_identifier = os.path.join(experiment_name, env_name)
        self.run_dir = os.path.join("output/runs", run_identifier)
        self.log_dir = os.path.join(self.run_dir, "logs")
        self.video_dir = os.path.join(self.run_dir, "recordings")
        self.pvm_dir = os.path.join(self.run_dir, "pvms")
        self.model_dir = os.path.join(self.run_dir, "trained_models")
        if isinstance(self.envs[0], FixedFovealEnv):
            self.model_dir = os.path.join(self.model_dir, "no_pause")

        # Init text logging logging
        os.makedirs(self.log_dir, exist_ok=True)
        self.log_file = os.path.join(self.log_dir, f"seed{self.seed}.txt")

        # Log pause cost
        if isinstance(self.env, PauseableFixedFovealEnv):
            self._log("---\nTraining start with pause costs" + \
                str([env.pause_cost for env in self.envs]))

        # Load existing run if there is one
        if os.path.exists(self.model_dir):
            seeded_models = list(filter(
                lambda s: f"seed{self.seed}" in s, os.listdir(self.model_dir)))
            if len(seeded_models) > 0:
                self._log("Loading existing checkpoint")
                timesteps = [int(model.split("_")[1][4:]) for model in seeded_models]
                latest_model = seeded_models[np.argmax(timesteps)]
                self.load_checkpoint(f"{self.model_dir}/{latest_model}")
                n += self.current_timestep

        # Start acting in the environment
        self.start_time = time.time()
        obs, infos = self.env.reset()
        self.pvm_buffer.append(obs)
        pvm_obs = self.pvm_buffer.get_obs(mode="stack_max")

        # Init return value
        eval_returns = []

        while self.current_timestep < n:
            # Chose action from q network
            self.epsilon = self._epsilon_schedule(n)
            motor_actions, sensory_action_indices = self.q_network.chose_action(
                self.env, pvm_obs, self.epsilon, self.device)

            # Transform the action to an absolute fovea position
            sensory_actions = np.array(
                [self.sensory_action_set[i] for i in sensory_action_indices])

            # Perform the action in the environment
            next_pvm_obs, rewards, dones, _ = self._step(
                self.env,
                self.pvm_buffer,
                motor_actions,
                sensory_actions,
            )

            # Add new pvm ovbervation to the buffer
            self.rb.add(pvm_obs, next_pvm_obs, motor_actions, sensory_action_indices,
                        rewards, dones, {})
            pvm_obs = next_pvm_obs

            # Only train if a full batch is available
            self.current_timestep += self.n_envs
            if self.current_timestep > self.batch_size:

                # Save the model every 1M timesteps
                if (not self.no_model_output) and self.current_timestep % 1000000 == 0:
                    self._save_output(self.model_dir, "pt", self.save_checkpoint)

                # Training
                if self.current_timestep % self.train_frequency == 0:
                    self.train()

                # Evaluation
                if (self.current_timestep % self.eval_frequency == 0 and
                    self.eval_frequency > 0) or (self.current_timestep >= n):
                    eval_returns, out_paths, auc = self.evaluate()

                # Test against Atari-HEAD gaze predictor
                if not self.score_target and self.current_timestep % 1000 == 0:
                    eval_returns, out_paths, auc = self.evaluate(file_output=False)
                    train.report({"auc": auc})

        self.env.close()

        return eval_returns, out_paths

    def train(self):
        """
        Performs one training iteration from the replay buffer
        """
        # Replay buffer sampling
        # Counter-balance the true global transitions used for training
        data = self.rb.sample(self.batch_size // self.n_envs)

        # SFN training
        observation_quality = self._train_sfn(data)

        # DQN training
        if self.current_timestep > self.learning_start:
            self._train_dqn(data, observation_quality)

    def evaluate(self, file_output = True):
        # Set networks to eval mode
        self.q_network.eval()
        self.sfn.eval()

        episode_infos, out_paths = [], []
        for eval_ep in range(self.n_evals):
            # Create env
            eval_env = self.eval_env_generator(eval_ep)
            single_eval_env: Union[FixedFovealEnv, PauseableFixedFovealEnv] = \
                eval_env.envs[0] if isinstance(eval_env, VectorEnv) else eval_env
            n_eval_envs = eval_env.num_envs if isinstance(eval_env, VectorEnv) else 1

            # Init env
            obs, _ = eval_env.reset()
            done = False
            eval_pvm_buffer = PVMBuffer(
                self.pvm_stack,
                (n_eval_envs, self.frame_stack, *self.obs_size)
            )
            eval_pvm_buffer.append(obs)
            pvm_obs = eval_pvm_buffer.get_obs(mode="stack_max")

            # One episode in the environment
            while not done:
                # Chose an action from the Q network
                motor_actions, sensory_action_indices \
                    = self.q_network.chose_eval_action(pvm_obs, self.device)

                # Forcefully do a pause some of the time in debug mode
                if self.debug and np.random.choice([False, True], p=[0.9, 0.1]):
                    motor_actions = np.full(
                        motor_actions.shape, single_eval_env.pause_action)

                # Translate the action to an absolute fovea position
                sensory_actions = np.array(
                    [self.sensory_action_set[i] for i in sensory_action_indices])

                # Perform the action in the environment
                next_pvm_obs, rewards, dones, infos = self._step(
                    eval_env,
                    eval_pvm_buffer,
                    motor_actions,
                    sensory_actions,
                    eval=True
                )
                done = dones[0]
                pvm_obs = next_pvm_obs

            episode_infos.append(infos['final_info'][0])

            if file_output:
                # Save a visualization of the pvm buffer at the end of the episode
                if (not self.no_pvm_visualization):
                    self._save_output(
                        self.pvm_dir, "png", eval_pvm_buffer.to_png, eval_ep)

                # Save results as video and csv file
                # Only save 1/4th of the evals as videos
                if (self.capture_video) and single_eval_env.record and eval_ep % 4 == 0:
                    save_fn = single_eval_env.save_record_to_file \
                        if isinstance(single_eval_env, FixedFovealEnv) \
                        else single_eval_env.prev_episode.save
                    out_paths.append(self._save_output(
                        self.video_dir, "", save_fn, eval_ep))

                # Safe the model file in the first eval run
                if (not self.no_model_output) and eval_ep == 0:
                    self._save_output(
                        self.model_dir, "pt", self.save_checkpoint, eval_ep)

            elif isinstance(single_eval_env, PauseableFixedFovealEnv) \
                and self.evaluator:
                loader = GazeDataset.from_game_data(
                    [single_eval_env.prev_episode]).to_loader()
                self.auc = self.evaluator.eval(loader)["auc"]

            eval_env.close()

        # Log results
        self._log_eval_episodes(episode_infos)

        # Set the networks back to training mode
        self.q_network.train()
        self.sfn.train()

        eval_returns: List[float] = [
            episode_info["raw_reward"] for episode_info in episode_infos]
        return eval_returns, out_paths, self.auc

    def save_checkpoint(self, file_path: str):
        torch.save(
            {
                "sfn": self.sfn.state_dict(),
                "q": self.q_network.state_dict(),
                "training_steps": self.current_timestep
            },
            file_path
        )

    def load_checkpoint(self, file_path: str):
        checkpoint = torch.load(file_path, weights_only=True)
        self.sfn.load_state_dict(checkpoint["sfn"])
        self.q_network.load_state_dict(checkpoint["q"])
        self.current_timestep = checkpoint["training_steps"]

    def _log(self, s: str):
        """
        Own print function. logging module does not work with the current gymnasium
        installation for some reason.
        """
        assert self.log_file, "self._log needs self.log_file to bet set"
        with open(self.log_file, "a") as f:
            f.write(f"\n{s}")

    def _save_output(self, output_dir: str, file_prefix: str,
                     save_fn: Callable[[str], None], eval_ep: int = 0):
        """
        Saves different types of eval output to the file system in the context of the
        current episode
        """
        os.makedirs(output_dir, exist_ok=True)
        file_name = ((
            f"seed{self.seed}_step{self.current_timestep:07d}"
            f"_eval{eval_ep:02d}"))
        if isinstance(self.env, FixedFovealEnv):
            file_name = (
                f"seed{self.seed}_step{self.current_timestep:07d}"
                f"_eval{eval_ep:02d}_no_pause")
        if file_prefix: file_name += f".{file_prefix}"
        else: os.makedirs(file_name, exist_ok=True)
        out_path = os.path.join(output_dir, file_name)
        save_fn(out_path)
        return out_path

    def _step(self, env: VectorEnv, pvm_buffer: PVMBuffer, motor_actions: np.ndarray,
              sensory_actions: np.ndarray, eval = False):
        """
        Given an action, the agent does one step in the environment,
        returning the next observation

        :param Array[n_envs] motor_actions: Numpy array containing motor action for all
            parallel training envs.
        """
        # Take an action in the environment
        next_obs, rewards, dones, _, infos = env.step({
            "motor_action": motor_actions,
            "sensory_action": sensory_actions
        })

        # Log episode returns and handle `terminal_observation`
        if not eval and "final_info" in infos and True in dones:
            finished_env_idx = np.argmax(dones)
            self._log_episode(infos['final_info'][finished_env_idx])
            next_obs[finished_env_idx] = infos["final_observation"][finished_env_idx]

        # Update the latest observation in the pvm buffer
        assert len(env.envs) == 1, \
            "Vector env with more than one env not supported for the following code"
        if isinstance(env.envs[0], PauseableFixedFovealEnv) \
            and motor_actions[0] == env.envs[0].pause_action:
            pvm_buffer.buffer[-1] = np.expand_dims(np.max(np.vstack(
                [pvm_buffer.buffer[-1], next_obs]), axis=0), axis=0)
        else:
            pvm_buffer.append(next_obs)

        # Get the next pvm observation
        next_pvm_obs = pvm_buffer.get_obs(mode="stack_max")

        return next_pvm_obs, rewards, dones, infos

    def _log_episode(self, episode_info: EpisodeInfo):
        # Prepare the episode infos for the different supported envs
        if isinstance(self.envs[0], FixedFovealEnv):
            episode_info["pauses"], episode_info['pause_cost'] = 0, 0
            episode_info["no_action_pauses"], episode_info['prevented_pauses'] = 0, 0
            episode_info["raw_reward"] = episode_info["reward"]
            prevented_pause_counts = [0] * len(self.envs)
        elif isinstance(self.envs[0], PauseableFixedFovealEnv):
            prevented_pause_counts = [
                env.episode_info["prevented_pauses"] for env in self.envs]
        else:
            raise ValueError(f"Environment '{self.envs[0]}' not supported")

        prevented_pauses_warning = \
            f"\nWARNING: [Prevented Pauses: {episode_info['prevented_pauses']}]" \
                if episode_info['prevented_pauses'] else ""

        self._log((
            f"[T: {time.time()-self.start_time:.2f}] "
            f"[N: {self.current_timestep:07,d}] "
            f"[R, Raw R: {episode_info['reward']:.2f}, "
            f"{episode_info['raw_reward']:.2f}] "
            f"[Pauses: {episode_info['pauses']}] "
            f"{prevented_pauses_warning}"
        ))
        # Log the amount of prevented pauses over the entire learning period
        if not all(prevented_pause_counts) == 0:
            prev_pauses = ",".join(map(str, prevented_pause_counts))
            self._log(f"WARNING: [Prevented Pauses: {prev_pauses}]")

        # Ray logging
        ray_info = {
            "episode_reward": episode_info["raw_reward"],
            "sfn_loss": self.sfn_loss.item(),
            "k_timesteps": self.current_timestep / 1000
        }
        if isinstance(self.envs[0], PauseableFixedFovealEnv):
            ray_info.update({
                "pauses": episode_info["pauses"],
                "prevented_pauses": episode_info["prevented_pauses"],
                "no_action_pauses": episode_info["no_action_pauses"],
                "saccade_cost": episode_info["saccade_cost"],
                "pause_reward": episode_info["reward"],
                "auc": self.auc
            })
        train.report(ray_info)

    def _log_eval_episodes(self, episode_infos: List[dict]):
        # Unpack episode_infos
        episodic_returns, episode_lengths = [], []
        pause_counts, prevented_pauses = [], []
        no_action_pauses, raw_episodic_returns = [], []
        for episode_info in episode_infos:

            # Prepare the episode infos for the different supported envs
            if isinstance(self.envs[0], FixedFovealEnv):
                full_info = EpisodeInfo.new()
                full_info.update(episode_info)
                episode_info = full_info
                episode_info["timestep"] = episode_info["ep_len"]

            episodic_returns.append(episode_info["reward"])
            raw_episodic_returns.append(episode_info["raw_reward"])
            episode_lengths.append(episode_info["timestep"])
            pause_counts.append(episode_info["pauses"])
            prevented_pauses.append(episode_info["prevented_pauses"])
            no_action_pauses.append(episode_info["no_action_pauses"])

        pause_cost = 0 if isinstance(self.envs[0], FixedFovealEnv) \
            else self.envs[0].pause_cost

        # Log everything
        prevented_pauses_warning = "" if all(n == 0 for n in prevented_pauses) else \
            f"\nWARNING: [Prevented Pauses]: {','.join(map(str, prevented_pauses))}"
        self._log((
            f"[N: {self.current_timestep:07,d}]"
            f" [Eval Return, Raw Eval Return: {np.mean(episodic_returns):.2f}+/-"
                f"{np.std(episodic_returns):.2f}"
                f", {np.mean(raw_episodic_returns):.2f}+/-"
                f"{np.std(raw_episodic_returns):.2f}]"
            f"\n[Returns: {','.join([f'{r:.2f}' for r in episodic_returns])}]"
            f"\n[Episode Lengths: {','.join([f'{r:.2f}' for r in episode_lengths])}]"
            f"\n[Pauses: {','.join([str(n) for n in pause_counts])} with cost "
            f"{pause_cost}]{prevented_pauses_warning}"
        ))

    def _train_sfn(self, data):
        # Prediction
        concat_observation = torch.concat(
            [data.next_observations, data.observations], dim=1)
        pred_motor_actions = self.sfn(Resize(self.obs_size)(concat_observation))
        self.sfn_loss = self.sfn.get_loss(
            pred_motor_actions, data.motor_actions.flatten())

        # Back propagation
        self.sfn_optimizer.zero_grad()
        self.sfn_loss.backward()
        self.sfn_optimizer.step()

        # Return the probabilites the sfn would have also selected the truely selected
        # action, given the limited observation. Higher probabilities suggest
        # better information was provided from the visual input
        observation_quality = F.softmax(pred_motor_actions, dim=0).gather(
            1, data.motor_actions).squeeze().detach()

        return observation_quality

    def _train_dqn(self, data, observation_quality):
        """
        Trains the behavior q network and copies it to the target q network with
        self.target_network_frequency.

        :param NDArray data: A sample from the replay buffer
        :param NDArray[Shape[self.batch_size], Float] observation_quality: A batch of
            probabilities of the SFN predicting the action that the agent selected
        """
        # Target network prediction
        with torch.no_grad():
            # Assign a value to every possible action in the next state for one batch
            # motor_target.shape: [32, 19]
            motor_target, sensory_target = self.target_network(
                Resize(self.obs_size)(data.next_observations))
            # Get the maximum action value for one batch
            # motor_target_max.shape: [32]
            motor_target_max, _ = motor_target.max(dim=1)
            sensory_target_max, _ = sensory_target.max(dim=1)
            # Scale step-wise reward with observation_quality
            observation_quality_adjusted = observation_quality.clone()
            observation_quality_adjusted[data.rewards.flatten() > 0] = \
                1 - observation_quality_adjusted[data.rewards.flatten() > 0]
            td_target = data.rewards.flatten() \
                - (1 - observation_quality) * self.sugarl_r_scale \
                + self.gamma * (motor_target_max + sensory_target_max) * (
                    1 - data.dones.flatten())
            original_td_target = data.rewards.flatten() + self.gamma * (
                motor_target_max + sensory_target_max) * (1 - data.dones.flatten())

        # Q network prediction
        old_motor_q_val, old_sensory_q_val = self.q_network(
            Resize(self.obs_size)(data.observations))
        old_motor_val = old_motor_q_val.gather(1, data.motor_actions).squeeze()
        old_sensory_val = old_sensory_q_val.gather(1, data.sensory_actions).squeeze()
        old_val = old_motor_val + old_sensory_val

        # Back propagation
        loss_without_sugarl = F.mse_loss(original_td_target, old_val)
        loss = F.mse_loss(td_target, old_val)
        backprop_loss = loss_without_sugarl if self.ignore_sugarl else loss
        self.optimizer.zero_grad()
        backprop_loss.backward()
        self.optimizer.step()

        # Update the target network with self.target_network_frequency
        if (self.current_timestep // self.n_envs) % self.target_network_frequency == 0:
            self.target_network.load_state_dict(self.q_network.state_dict())

    def _epsilon_schedule(self, total_timesteps: int):
        """
        Maps the current number of timesteps to a value of epsilon.
        """
        return linear_schedule(*self.epsilon_interval,
            self.exploration_fraction * total_timesteps, self.current_timestep)
