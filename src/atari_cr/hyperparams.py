from ray import train, tune
from typing import Optional, TypedDict

from ray.tune.search.optuna import OptunaSearch
from ray.tune.schedulers import ASHAScheduler

from atari_cr.agents.dqn_atari_cr.main import main, ArgParser
from atari_cr.atari_head.dataset import GazeDataset
from atari_cr.atari_head.gaze_predictor import GazePredictor

class ConfigParams(TypedDict):
    no_action_pause_cost: float
    pause_cost: float
    pvm_stack: int
    sensory_action_space_quantization: int
    saccade_cost_scale: float

def tuning(config: ConfigParams, time_steps: int, debug = False,
           gaze_target = True):
    # Copy quantization value into the two corresponding values
    if "sensory_action_space_quantization" in config:
        quantization = config.pop("sensory_action_space_quantization")
        config["sensory_action_x_space"] = quantization
        config["sensory_action_y_space"] = quantization

    # Add basic config
    args_dict = {}
    args_dict.update({
        "clip_reward": True,
        "capture_video": True,
        "total_timesteps": time_steps,
        "no_pvm_visualization": True,
        "no_model_output": True,
        "use_pause_env": True,
        "env": "ms_pacman"
    })

    # Other args
    args_dict.update({
        "debug": debug,
        "gaze_target": gaze_target,
        "evaluator":
            "/home/niko/Repos/atari-cr/output/atari_head/ms_pacman/drout0.3/999/checkpoint.pth"})

    # Add already found hyper params
    args_dict.update({
        "action_repeat": 5,
        "fov_size": 20,
        "sensory_action_space_quantization": 4, # from 9-16
        "pvm_stack": 16, # from 9-16
        "saccade_cost_scale": 0.0015, # from 9-16
        "no_action_pause_cost": 1.2, # from 10-23
        "pause_cost": 0.2, # from 9-16
    })

    # Add vaguely found hyperparameters
    args_dict.update({
        "no_action_pause_cost": 1.4,
        "pause_cost": 0.3,
        "pvm_stack": 13,
        "saccade_cost_scale": 0.0015,
    })

    # Add hyperparameter config
    args_dict.update(config)
    args_dict["exp_name"] = "tuning"

    # Run the experiment
    args = ArgParser().from_dict(args_dict)
    eval_returns, out_paths = main(args)

    # # Evaluate it
    # if gaze_predictor is None:
    #     # Send the current training result back to Tune
    #     result = {"episode_reward": sum(eval_returns) / len(eval_returns)}
    # else:
    #     # Train an agent and evaluate it regularly using gaze predictor
    #     dataset = GazeDataset.from_game_data(out_paths)
    #     agent_loader = dataset.to_loader()
    #     # Eval the gaze predictor on that dataset
    #     eval_result = gaze_predictor.eval(agent_loader)
    #     result = { "auc": eval_result["auc"] }

    # return result

if __name__ == "__main__":
    GAZE_TARGET = True
    DEBUG = False
    concurrent_runs = 3 if DEBUG else 4
    num_samples = 1 * concurrent_runs if DEBUG else 100
    time_steps = int(1e6) if DEBUG else int(1e6)

    trainable = tune.with_resources(
        lambda config: tuning(config, time_steps, DEBUG, GAZE_TARGET),
        {"cpu": 8//concurrent_runs, "gpu": 1/concurrent_runs})

    param_space: ConfigParams = {
        "pause_cost": tune.quniform(0.00, 0.03, 0.002),
        # "no_action_pause_cost": tune.quniform(0., 2.0, 0.1),
        "pvm_stack": tune.randint(1, 20),
        "sensory_action_space_quantization": tune.randint(1, 84),
        "saccade_cost_scale": tune.quniform(0.0000, 0.0050, 0.0005),
    }

    metric, mode = ("windowed_auc", "max") if GAZE_TARGET else ("episode_reward", "max")
    tuner = tune.Tuner(
        trainable,
        param_space=param_space,
        tune_config=tune.TuneConfig(
            num_samples=num_samples,
            scheduler=None if DEBUG else ASHAScheduler(
                stop_last_trials=False
            ),
            search_alg=OptunaSearch(),
            metric=metric,
            mode=mode
        ),
        run_config=train.RunConfig(
            storage_path="/home/niko/Repos/atari-cr/output/ray_results",
            # stop={"training_iteration": 5000}
        )
    )
    results = tuner.fit()
    print("Best result:\n", results.get_best_result().config)
