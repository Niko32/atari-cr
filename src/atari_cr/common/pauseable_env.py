from active_gym import FixedFovealEnv, AtariEnvArgs, AtariBaseEnv, RecordWrapper
from active_gym.atari_env import AtariEnv
import gymnasium as gym
from gymnasium.spaces import Dict, Discrete, Box
import cv2
import torch
import numpy as np
from typing import TypedDict, List, Tuple
import copy
from torchvision.transforms import Resize


class RecordBuffer(TypedDict):
    """
    Buffer saving the env history for one episode
    """
    rgb: List[np.ndarray]
    state: List[np.ndarray]
    action: List[np.ndarray]
    reward: List[np.ndarray]
    done: List[np.ndarray]
    truncated: List[np.ndarray]
    info: List[np.ndarray]
    return_reward: List[np.ndarray]
    episode_pauses: List[int]
    fov_loc: List[np.ndarray]
    fov_size: List[np.ndarray]

    def new():
        return dict((key, []) for key in RecordBuffer.__annotations__)        

class PauseableFixedFovealEnv(gym.Wrapper):
    """
    Environemt making it possible to be paused to only take
    a sensory action without progressing the game.
    """
    def __init__(self, env: gym.Env, args, pause_cost = 0.01, successive_pause_limit = 20):
        """
        Parameters
        ----------
        pause_cost : float
            Negative reward for the agent whenever they chose to not take
            an action in the environment to only look; prevents abuse of pausing
        successive_pause_limit : int
            Limit to the amount of successive pauses the agent can make before
            a random action is selected instead. This prevents the agent from halting
        """
        super().__init__(env)
        self.fov_size: Tuple[int, int] = args.fov_size
        self.fov_init_loc: Tuple[int, int] = args.fov_init_loc
        assert (np.array(self.fov_size) < np.array(self.obs_size)).all()

        self.sensory_action_mode: str = args.sensory_action_mode 
        if self.sensory_action_mode == "relative":
            self.sensory_action_space = np.array(args.sensory_action_space)
        elif self.sensory_action_mode == "absolute":
            self.sensory_action_space = np.array(self.obs_size) - np.array(self.fov_size)
        else:
            raise ValueError("sensory_action_mode needs to be either 'absolute' or 'relative'")

        self.resize: Resize = Resize(self.env.obs_size) if args.resize_to_full else None
        self.mask_out: bool = args.mask_out

        self.action_space = Dict({
            # One additional action lets the agent stop the game to perform a 
            # sensory action without the game progressing  
            "motor": Discrete(self.env.action_space.n + 1),
            "sensory": Box(low=self.sensory_action_space[0], 
                                 high=self.sensory_action_space[1], dtype=int),
        })
        # Whether to pause the game at the current step
        self.pause_action = False

        # Count and log the number of pauses made and their cost
        self.pause_cost = pause_cost
        self.successive_pause_limit = successive_pause_limit
        self.n_pauses = 0

        # Count successive pause actions to prevent the system
        # from halting
        self.successive_pauses = 0
        self.prevented_pauses = 0

        # Attributes from RecordWrapper class
        self.args = args
        self.cumulative_reward = 0
        self.ep_len = 0
        self.record_buffer: RecordBuffer = None
        self.prev_record_buffer: RecordBuffer = None
        self.record = args.record
        if self.record:
            self._reset_record_buffer()

        self.env: AtariEnv

    def step(self, action):
        # The pause action is the last action in the action set
        prev_pause_action = self.pause_action
        self.pause_action = self._is_pause(action["motor"])
        if self.pause_action:
            # Disallow a pause on the first episode step because there is no
            # observation to look at yet
            if not hasattr(self, "state"):
                action["motor"] = np.random.randint(1, len(self.env.actions))
                return self.step(action)

            # Prevent the agent from being stuck on only using pauses
            # Perform a random motor action instead if too many pauses
            # have happened in a row
            if self.prevented_pauses > 50 or self.successive_pauses > self.successive_pause_limit:
                while self.pause_action:
                    action["motor"] = self.action_space["motor"].sample()
                    self.pause_action = self._is_pause(action["motor"])
                self.pause_action = False
                self.prevented_pauses += 1
                return self.step(action)

            # Log another pause
            self.n_pauses += 1
            if prev_pause_action:
                self.successive_pauses += 1
            else:
                self.successive_pauses = 0
                
            # Only make a sensory step with a small cost
            reward, done, truncated = -self.pause_cost, False, False
            info = { "raw_reward": reward }
            fov_state = self._fov_step(full_state=self.state, action=action["sensory"])

        else:
            self.successive_pauses = 0
            # Normal step
            state, reward, done, truncated, info = self.env.step(action=action["motor"])
            # Safe the state for the next sensory step
            self.state = state
            # Sensory step
            fov_state = self._fov_step(full_state=self.state, action=action["sensory"])  

        # RecordWrapper.step code
        self.ep_len += 1
        self.cumulative_reward += reward
        info = self._update_info(info)
        info["pause_cost"] = self.pause_cost
        info["n_pauses"] = self.n_pauses
        info["prevented_pauses"] = self.prevented_pauses
        info["fov_loc"] = self.fov_loc.copy()

        if self.record:
            rgb = self.env.render()
            self._save_transition(self.state, 
                action, self.cumulative_reward, 
                done, truncated, info, rgb=rgb, 
                return_reward=reward, 
                episode_pauses=self.n_pauses,
                fov_loc=self.fov_loc,
                fov_size=self.fov_size
            )

            if not done:
                self.record_buffer["fov_loc"].append(info["fov_loc"])

        # Reset the number of pauses and prevented pauses for the next episode
        if done:
            self.n_pauses = 0
            self.prevented_pauses = 0

        return fov_state, reward, done, truncated, info
    
    def reset(self):
        full_state, info = self.env.reset()

        self.cumulative_reward = 0
        self.ep_len = 0
        self.fov_loc = np.rint(np.array(self.fov_init_loc, copy=True)).astype(np.int32)
        fov_state = self._get_fov_state(full_state)
        info["fov_loc"] = self.fov_loc.copy()
        info = self._update_info(info)

        if self.record:
            # Reset record_buffer
            self.record_buffer["fov_size"] = self.fov_size
            self.record_buffer["fov_loc"] = [info["fov_loc"]]
            # Record Wrapper Stuff
            rgb = self.env.render()
            self._reset_record_buffer()
            self._save_transition(fov_state, done=False, info=info, rgb=rgb)

        return fov_state, info

    def save_record_to_file(self, file_path: str, draw_focus = True):
        # TODO: Draw the number of pauses onto the screen for better debugging
        # Pauses just look like lag in the video. 
        # TODO: Investigate why the fovea is appearently not moved when pausing
        if self.record:
            video_path = file_path.replace(".pt", ".mp4")
            self.prev_record_buffer: RecordBuffer
            size = self.prev_record_buffer["rgb"][0].shape[:2][::-1]
            fps = 30
            video_writer = cv2.VideoWriter(video_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
            for i, frame in enumerate(self.prev_record_buffer["rgb"]):
                if draw_focus:
                    y_loc, x_loc = self.prev_record_buffer["fov_loc"][i]
                    fov_size = self.prev_record_buffer["fov_size"]

                    # The fov_loc is set within a 84x84 grid while the video output is 256x256
                    # To scale them accordingly we multiply with the following
                    COORD_SCALING = 256 / 84
                    x_loc = int(x_loc * COORD_SCALING)
                    y_loc = int(y_loc * COORD_SCALING)
                    fov_size = (int(fov_size[0] * COORD_SCALING), int(fov_size[1] * COORD_SCALING))

                    top_left = (x_loc, y_loc)
                    bottom_right = (x_loc + fov_size[0], y_loc + fov_size[1])
                    color = (255, 0, 0)
                    thickness = 1
                    frame = cv2.rectangle(frame, top_left, bottom_right, color, thickness)
                video_writer.write(frame)
            self.prev_record_buffer["rgb"] = video_path
            self.prev_record_buffer["state"] = [0] * len(self.prev_record_buffer["reward"])
            torch.save(self.prev_record_buffer, file_path)
            video_writer.release()

    def _is_pause(self, motor_action: int):
        """
        Checks if a given motor action is the pause action
        """
        return motor_action == len(self.env.actions)

    def _update_info(self, info):
        info["reward"] = self.cumulative_reward
        info["ep_len"] = self.ep_len
        return info

    def _clip_to_valid_fov(self, loc):
        return np.rint(np.clip(loc, 0, np.array(self.env.obs_size) - np.array(self.fov_size))).astype(int)

    def _clip_to_valid_sensory_action_space(self, action):
        return np.rint(np.clip(action, *self.sensory_action_space)).astype(int)

    def _fov_step(self, full_state, action):
        if type(action) is torch.Tensor:
            action = action.detach().cpu().numpy()
        elif type(action) is Tuple:
            action = np.array(action)

        if self.sensory_action_mode == "absolute":
            action = self._clip_to_valid_fov(action)
            self.fov_loc = action
        elif self.sensory_action_mode == "relative":
            action = self._clip_to_valid_sensory_action_space(action)
            fov_loc = self.fov_loc + action
            self.fov_loc = self._clip_to_valid_fov(fov_loc)

        fov_state = self._get_fov_state(full_state)
        
        return fov_state

    def _get_fov_state(self, full_state):
        fov_state = full_state[..., self.fov_loc[0]:self.fov_loc[0]+self.fov_size[0],
                                    self.fov_loc[1]:self.fov_loc[1]+self.fov_size[1]]

        if self.mask_out:
            mask = np.zeros_like(full_state)
            mask[..., self.fov_loc[0]:self.fov_loc[0]+self.fov_size[0],
                    self.fov_loc[1]:self.fov_loc[1]+self.fov_size[1]] = fov_state
            fov_state = mask
        elif self.resize:
            fov_state = self.resize(torch.from_numpy(fov_state))
            fov_state = fov_state.numpy()

        return fov_state

    def _reset_record_buffer(self):
        self.prev_record_buffer = copy.deepcopy(self.record_buffer)
        self.record_buffer = RecordBuffer.new()

    def _save_transition(self, state, action=None, reward=None, done=None, 
            truncated=None, info=None, rgb=None, return_reward=None, 
            episode_pauses=None, fov_size=None, fov_loc=None):
        if (done is not None) and (not done):
            self.record_buffer["state"].append(state)
            self.record_buffer["rgb"].append(rgb)

        if done is not None and len(self.record_buffer["state"]) > 1:
            self.record_buffer["done"].append(done) 
        if info is not None and len(self.record_buffer["state"]) > 1:
            self.record_buffer["info"].append(info)

        if action is not None:
            self.record_buffer["action"].append(action)
        if reward is not None:
            self.record_buffer["reward"].append(reward)
        if truncated is not None:
            self.record_buffer["truncated"].append(truncated)
        if return_reward is not None:
            self.record_buffer["return_reward"].append(return_reward)
        if episode_pauses is not None:
            self.record_buffer["episode_pauses"].append(episode_pauses)


class SlowableFixedFovealEnv(PauseableFixedFovealEnv):
    """
    Environemt making it possible to be paused to only take
    a sensory action without progressing the game.
    Additionally, the game can be run at 1/3 of the original
    speed as it was done with the Atari-HEAD dataset
    (http://arxiv.org/abs/1903.06754)
    """
    def __init__(self, env: gym.Env, args):
        super().__init__(env, args)
        self.ms_since_motor_step = 0

    def step(self, action):
        pause_action = action["motor"] == len(self.env.actions)
        if pause_action and (self.state is not None):
            # If it has been more than 50ms (results in 20 Hz) since the last motor action 
            # NOOP will be chosen as a motor action instead of continuing the pause
            SLOWED_FRAME_RATE = 20
            if self.ms_since_motor_step >= 1000/SLOWED_FRAME_RATE:
                action["motor"] = np.int64(0)
                return self.step(action)
            
        fov_state, reward, done, truncated, info = super().step(action)

        # Progress the time because a vision step has happened
        # TODO: depending on how big the sensory action was
        # Time for a saccade is fixed to 20 ms for now
        self.ms_since_motor_step += 20

        return fov_state, reward, done, truncated, info