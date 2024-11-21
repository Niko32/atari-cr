import unittest
from sys import argv

from atari_cr.agents.dqn_atari_cr.main import main, ArgParser

class TestMain(unittest.TestCase):
    def test_pauseable(self):
        argv.extend([
            "--clip_reward",
                "--capture_video",
                "--env", "ms_pacman",
                "--exp_name", "test_config",
                "--total_timesteps", "10000",
                "--learning_start", "1000",
                "--debug",
                "--pause_cost", "0.05",
                "--use_pause_env",
                "--action_repeat", "5",
                "--evaluator",
                "/home/niko/Repos/atari-cr/output/atari_head/ms_pacman/drout0.3/999/checkpoint.pth"
        ])

        args = ArgParser().parse_args(known_only=True)
        main(args)

    def test_og(self):
        argv.extend([
            "--clip_reward",
            "--capture_video",
            "--exp_name", "test_og",
            "--total_timesteps", "5000",
            "--learning_start", "100",
            "--eval_num", "3",
            #
            "--no_pvm_visualization",
            "--no_model_output",
        ])

        args = ArgParser().parse_args(known_only=True)
        main(args)

if __name__ == '__main__':
    unittest.main()