from active_gym.atari_env import AtariEnv
import gymnasium as gym
from gymnasium.spaces import Dict, Discrete, Box
import torch
import numpy as np
from typing import Optional, Tuple
from torchvision.transforms import Resize
from active_gym import AtariEnvArgs

from atari_cr.models import EpisodeInfo, EpisodeRecord, StepInfo, FovType
from atari_cr.utils import EMMA_fixation_time
from atari_cr.atari_head.utils import VISUAL_DEGREE_SCREEN_SIZE
from atari_cr.atari_head.dataset import SCREEN_SIZE
from atari_cr.graphs.eccentricity import A, B, C

MASKED_ACTION_PENTALTY = -1e9 # Negative reward for masking out actions


class PauseableFixedFovealEnv(gym.Wrapper):
    """
    Environemt wrapper making it possible to be paused to only take
    a sensory action without progressing the game.

    :param float pause_cost: Negative reward for the agent whenever they chose to
        not take an action in the environment to only look; prevents abuse of
        pausing
    :param int consecutive_pause_limit: Limit to the amount of consecutive pauses
        the agent can make before a random action is selected instead. This prevents
        the agent from halting
    :param float saccade_cost_scale: How much the agent is punished for bigger eye
        movements
    :param bool no_pause: Whether to disable the pause action
    """
    def __init__(self, env: AtariEnv, args: AtariEnvArgs, pause_cost = 0.01,
            saccade_cost_scale = 0.001, fov: FovType = "window", no_pauses = False,
            consecutive_pause_limit = 20):
        super().__init__(env)
        self.fov_size: Tuple[int, int] = args.fov_size
        self.fov: FovType = fov
        self.fov_init_loc: Tuple[int, int] = args.fov_init_loc
        assert (np.array(self.fov_size) <
                np.array(env.obs_size)).all()
        self.no_pauses = no_pauses

        # Get sensory action space for the sensory action mode
        self.relative_sensory_actions = args.sensory_action_mode == "relative"
        if self.relative_sensory_actions:
            self.sensory_action_space = np.array(args.sensory_action_space)
        elif self.fov in ["gaussian", "exponential"]:
            self.sensory_action_space = np.array(env.obs_size)
        elif self.fov == "window":
            self.sensory_action_space = \
                np.array(env.obs_size) - np.array(self.fov_size)

        self.resize: Resize = Resize(env.obs_size) \
            if args.resize_to_full else None

        self.action_space = Dict({
            # One additional action lets the agent stop the game to perform a
            # sensory action without the game progressing
            "motor_action": Discrete(
                env.action_space.n if no_pauses else env.action_space.n + 1),
            "sensory_action": Box(low=self.sensory_action_space[0],
                                 high=self.sensory_action_space[1], dtype=int),
        })
        self.pause_action = None if no_pauses else \
            self.action_space["motor_action"].n - 1

        # How much the agent is punished for large eye movements
        self.saccade_cost_scale = saccade_cost_scale

        # Whether the current action is a pause action
        self.prev_pause_action = 0

        # Count and log the number of pauses made and their cost
        self.pause_cost = pause_cost
        self.consecutive_pause_limit = consecutive_pause_limit

        # Attributes for recording episodes
        self.prev_episode: Optional[EpisodeRecord] = None
        self.record = args.record
        self.episode_info: EpisodeInfo
        self.env: AtariEnv

    def reset(self):
        self.state, info = self.env.reset()
        assert info == {'raw_reward': 0}
        assert self.state.shape == (4, 84, 84)
        assert self.state.dtype == np.float64

        self.frames = []
        self.obs = []
        self.step_infos = []
        self.episode_info = EpisodeInfo.new()
        self.timestep = -1
        self.consecutive_pauses = 0
        self.fov_loc = np.array(self.fov_init_loc, copy=True).astype(np.int32)

        fov_obs = self._crop_observation(self.state)

        return fov_obs, info

    def step(self, action):
        # Log the state before the action is taken
        step_info = StepInfo.new()
        self.timestep += 1
        step_info["timestep"] = self.timestep
        step_info["fov_loc"] = self.fov_loc.tolist()
        step_info["motor_action"] = action["motor_action"]
        step_info["sensory_action"] = action["sensory_action"]
        step_info["pauses"] = int(action["motor_action"] == self.pause_action)
        self.frames.append(self.unwrapped.render())

        # Save the previous fov_loc to calculate the saccade cost
        prev_fov_loc = self.fov_loc.copy()

        # Disallow a pause on the first episode step because there is no
        # observation to look at yet and do a random action instead
        if not hasattr(self, "state") and step_info["pauses"]:
            action["motor_action"] = self._sample_no_pause()
            step_info["pauses"] = int(action["motor_action"] == self.pause_action)
            assert not step_info["pauses"], \
                "Pause action should not happen on the first episode step"

        # Actual pause step
        if step_info["pauses"]:
            raw_reward = 0
            done, truncated = False, False
            info = {"raw_reward": raw_reward}

            if self.consecutive_pauses > self.consecutive_pause_limit:
                # Too many pauses in a row
                step_info["reward"] = MASKED_ACTION_PENTALTY
                step_info["prevented_pauses"] = 1
                truncated = True
            elif np.all(self.fov_loc == action["sensory_action"]):
                # No action pause
                step_info["reward"] = MASKED_ACTION_PENTALTY
                step_info["no_action_pauses"] = 1
            else:
                # Normal pause
                step_info["reward"] = -self.pause_cost

            # Log another pause
            if self.prev_pause_action:
                self.consecutive_pauses += 1
            else:
                self.consecutive_pauses = 0
            # Set prev pause action for next step
            self.prev_pause_action = step_info["pauses"]

        else:
            self.consecutive_pauses = 0
            # Normal step, the state is saved for the next pause step
            self.state, step_info["reward"], done, truncated, info = \
                self.env.step(action=action["motor_action"])
            raw_reward = step_info["reward"]

        # Sensory step
        fov_state = self._fov_step(
            full_state=self.state, action=action["sensory_action"])

        # Add costs for the time it took the agent to move its fovea
        visual_degrees_per_pixel = np.array(VISUAL_DEGREE_SCREEN_SIZE) / \
            np.array(self.get_wrapper_attr("obs_size"))
        visual_degree_distance = np.sqrt(np.sum(
            np.square((self.fov_loc - prev_fov_loc) * visual_degrees_per_pixel) ))
        _, total_emma_time, fovea_did_move = EMMA_fixation_time(
            visual_degree_distance)
        step_info["emma_time"] = total_emma_time
        step_info["saccade_cost"] = self.saccade_cost_scale * total_emma_time
        step_info["reward"] -= step_info["saccade_cost"]

        # Log the results of taking an action
        step_info["reward_wo_sugarl"] = info["raw_reward"]
        step_info["raw_reward"] = raw_reward
        step_info["done"] = done
        step_info["truncated"] = truncated
        # Episode info stores cumulative sums of the step info keys
        for key in self.episode_info.keys():
            self.episode_info[key] += step_info[key]
        step_info["episode_info"] = self.episode_info.copy()
        step_info["consecutive_pauses"] = self.consecutive_pauses

        # Log the infos of all steps in one record for the
        # entire episode
        self.step_infos.append(step_info)
        if done or truncated:
            self.prev_episode = EpisodeRecord(
                np.stack(self.frames),
                EpisodeRecord.annotations_from_step_infos(self.step_infos),
                { "fov_size": self.fov_size, "fov": self.fov },
                np.stack(self.obs) if self.obs else None
            )

        return fov_state, step_info["reward"], done, truncated, step_info

    def add_obs(self, obs: np.ndarray):
        """ :param Array[4,84,84] obs: Frame stack, only last frame is saved """
        self.obs.append(obs[-1])

    def _sample_no_pause(self):
        """ Samples an action that is not a pause. """
        return np.random.randint(len(self.get_wrapper_attr("actions")))

    def _skip_step(self, reward):
        """
        Make a step without actually making a step.

        Returns
        -------
        state, the given reward, done, truncated, info
        """
        done, truncated = False, False
        info = { "raw_reward": reward }
        return self.state, reward, done, truncated, info

    def _clip_to_valid_fov(self, loc: np.ndarray):
        """ :param Array[W,H] loc: """
        return np.clip(loc, [0,0], self.sensory_action_space).astype(int)

    def _fov_step(self, full_state, action):
        """
        Changes self.fov_loc by the given action and returns a version of the full
        state that is cropped to where the new self.fov_loc is

        :returns Array[4,84,84]:
        """
        if type(action) is torch.Tensor:
            action = action.detach().cpu().numpy()
        elif type(action) is Tuple:
            action = np.array(action)

        # Move the fovea
        if self.relative_sensory_actions:
            action = self._clip_to_valid_fov(action)
            action = self.fov_loc + action
        self.fov_loc = self._clip_to_valid_fov(action)

        fov_state = self._crop_observation(full_state)

        return fov_state

    def _crop_observation(self, full_state: np.ndarray):
        """
        Get a version of the full_state that is cropped to the fovea around fov_loc if
        fov is set to 'window'. Otherwise get a mask over the output

        :param Array[4,84,84] full_state: Stack of four greyscale images of type float64
        """
        masked_state = np.zeros_like(full_state)
        if self.fov == "gaussian":
            distances_from_fov = self._pixel_eccentricities(
                full_state.shape[-2:], self.fov_loc)
            sigma = self.fov_size[0] / 2
            gaussian = np.exp(-np.sum(
                np.square(distances_from_fov) / (2 * np.square(sigma)),
                axis=0)) # -> [84,84]
            gaussian /= gaussian.max()
            masked_state = full_state * gaussian
        elif self.fov == "window":
            # Make this work with a list of fov_locs
            crop = full_state[...,
                self.fov_loc[0]:self.fov_loc[0] + self.fov_size[0],
                self.fov_loc[1]:self.fov_loc[1] + self.fov_size[1]
            ]
            # Fill the area outside the crop with zeros
            masked_state[...,
                self.fov_loc[0]:self.fov_loc[0] + self.fov_size[0],
                self.fov_loc[1]:self.fov_loc[1] + self.fov_size[1]
            ] = crop
        elif self.fov == "exponential":
            pixel_eccentricities = self._pixel_eccentricities(
                full_state.shape[-2:], self.fov_loc)
            # Convert from pixels to visual degrees
            eccentricities = (pixel_eccentricities.transpose([1,2,0]) *\
                (np.array(VISUAL_DEGREE_SCREEN_SIZE) / np.array(SCREEN_SIZE))
                ).transpose(2,0,1)
            # Absolute 1D distances
            abs_distances = np.sqrt(np.square(eccentricities).sum(axis=0))

            mask = A * np.exp(B * abs_distances) + C
            mask /= mask.max()
            masked_state = full_state * mask

        return masked_state

    @staticmethod
    def _pixel_eccentricities(screen_size: tuple[int, int], fov_loc: tuple[int, int]):
        """
        Returns a tensor containing x and y distances from the fov_loc for every
        possible x and y coord. Used for broadcasted calculations.

        :returns Array[2,W,H]:
        """
        x = np.arange(screen_size[0])
        y = np.arange(screen_size[1])
        mesh = np.stack(np.meshgrid(x, y, indexing="xy")) # -> [2,84,84]
        fov_loc = np.broadcast_to(np.array(
            fov_loc)[..., np.newaxis, np.newaxis], [2,*screen_size])
        return mesh - fov_loc # -> [2,84,84]
