from collections import deque
import os
import time
from typing import List
from sklearn.metrics import roc_auc_score
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, SubsetRandomSampler, RandomSampler
from torch.utils.tensorboard import SummaryWriter
import torch.optim as optim
import numpy as np
import h5py
from tap import Tap
from tqdm import tqdm

from atari_cr.atari_head.dataset import GazeDataset
from atari_cr.atari_head.utils import saliency_auc
from atari_cr.common.utils import gradfilter_ema, grid_image, show_tensor, grid_image2

class ArgParser(Tap):
    debug: bool = False # Debug mode for less data loading
    load_model: bool = False # Whether to load an existing model (if possible) or train a new one

class GazePredictionNetwork(nn.Module):
    """
    Neural network predicting a saliency map for a given stack of 4 greyscale atari game images.
    """
    def __init__(self):
        super(GazePredictionNetwork, self).__init__()
        
        # Convolutional layers
        self.conv2d_1 = nn.Conv2d(4, 32, kernel_size=8, stride=4)
        self.batch_normalization_1 = nn.BatchNorm2d(32)
        self.conv2d_2 = nn.Conv2d(32, 64, kernel_size=4, stride=2)
        self.batch_normalization_2 = nn.BatchNorm2d(64)
        self.conv2d_3 = nn.Conv2d(64, 64, kernel_size=3, stride=1)
        self.batch_normalization_3 = nn.BatchNorm2d(64)
        
        # Deconvolutional (transpose convolution) layers
        self.conv2d_transpose_1 = nn.ConvTranspose2d(64, 64, kernel_size=3, stride=1)
        self.batch_normalization_4 = nn.BatchNorm2d(64)
        self.conv2d_transpose_2 = nn.ConvTranspose2d(64, 32, kernel_size=4, stride=2)
        self.batch_normalization_5 = nn.BatchNorm2d(32)
        self.conv2d_transpose_3 = nn.ConvTranspose2d(32, 1, kernel_size=8, stride=4)
        
        # Softmax layer; Uses log softmax to conform to the KLDiv expected input
        self.log_softmax = nn.LogSoftmax(dim=1)
        self.dropout = nn.Dropout(0.0)

    def forward(self, x):
        # Convolutional layers
        x = self.conv2d_1(x)
        x = F.relu(x)
        x = self.batch_normalization_1(x)
        x = self.dropout(x)
        x = self.conv2d_2(x)
        x = F.relu(x)
        x = self.batch_normalization_2(x)
        x = self.dropout(x)
        x = self.conv2d_3(x)
        x = F.relu(x)
        x = self.batch_normalization_3(x)
        x = self.dropout(x)
        
        # Deconvolutional layers
        x = self.conv2d_transpose_1(x)
        x = F.relu(x)
        x = self.batch_normalization_4(x)
        x = self.dropout(x)
        x = self.conv2d_transpose_2(x)
        x = F.relu(x)
        x = self.batch_normalization_5(x)
        x = self.dropout(x)
        x = self.conv2d_transpose_3(x)
        
        # Reshape and apply softmax
        x = x.view(x.size(0), -1)
        x = self.log_softmax(x)
        x = x.view(x.size(0), 84, 84)
        
        return x
    
    @staticmethod
    def from_h5(save_path: str):
        f = h5py.File(save_path, 'r')

        model = GazePredictionNetwork()
        state_dict = model.state_dict()

        h5_weights = {}
        for key in f["model_weights"]:
            if len(f["model_weights"][key]) > 0:
                h5_weights[key] = f["model_weights"][key][key]

        for layer in h5_weights:
            for key in h5_weights[layer]:
                value = h5_weights[layer][key]
                key: str = key[:-2]
                key = key.replace("gamma", "weight") \
                    .replace("beta", "bias") \
                    .replace("moving", "running") \
                    .replace("variance", "var") \
                    .replace("kernel", "weight")

                if not isinstance(value[:], np.ndarray): breakpoint()
                value = torch.Tensor(value[:])
                value = value.permute(list(reversed(range(len(value.shape)))))
                key = key.replace("kernel", "weight")

                state_dict[f"{layer}.{key}"] = torch.Tensor(value)

        # OPTIONAL: Optimizer weights

        model.load_state_dict(state_dict)

        return model
    
class GazePredictor():
    """
    Wrapper around GazePredictionNetwork to handle training etc.
    """
    def __init__(
            self, 
            model: GazePredictionNetwork,
            dataset: GazeDataset,
            output_dir: str
        ):
        self.model = model
        self.train_loader, self.val_loader = self._init_data_loaders(dataset)
        self.output_dir = output_dir

        # Loss function, optimizer, compute device and tesorboard writer
        self.loss_function = nn.KLDivLoss(reduction="batchmean")
        self.optimizer = optim.Adadelta(self.model.parameters(), lr=1.0, rho=0.95, eps=1e-08, weight_decay=0.0)
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model.to(self.device)
        self.writer = SummaryWriter(os.path.join(output_dir, "tensorboard"))

        # Init Grokfast
        self.grads = None

        # Count the number of trained epochs
        self.epoch = 0

    def train(self, n_epochs: int):
        self.model.train()

        final_epoch = self.epoch + n_epochs
        losses = deque(maxlen=100)
        for self.epoch in range(self.epoch, final_epoch):

            print(f"Epoch {self.epoch + 1} / {final_epoch}")
            with tqdm(self.train_loader, colour="yellow") as t:
                for inputs, targets, _ in t:
                    inputs, targets = inputs.to(self.device), targets.to(self.device)

                    self.optimizer.zero_grad()
                    outputs = self.model(inputs)
                    loss = self.loss_function(outputs, targets)
                    loss.backward()
                    self.grads = gradfilter_ema(self.model, self.grads)
                    self.optimizer.step()

                    losses.append(loss.item())
                    t.set_postfix(loss=f"{np.mean(losses):6.4f}")
                    if t.n == t.total: t.colour = "green"

            if self.epoch % 10 == 9:
                self.save()

        self.model.eval()
        print('Training finished')
        
        # Save the trained model
        self.save()

    def eval(self):
        """
        :returns Tuple[float, float]: KL Divergence and AUC
        """
        kl_divs, aucs = [], []
        for frame_stack_batch, saliency_map_batch, _ in self.val_loader:
            # Prediction in log space as expected by KLDivLoss
            prediction = self.model(frame_stack_batch.to(self.device))
            saliency_map_batch = saliency_map_batch.to(self.device)

            kl_divs.append(nn.KLDivLoss()(prediction, saliency_map_batch).detach().cpu().numpy())
            aucs.append(saliency_auc(prediction.exp(), saliency_map_batch, self.device).cpu().numpy())

        return np.mean(kl_divs), np.mean(aucs)
    
    def save(self):
        save_path = f"{self.epoch + 1}.pth"
        torch.save(
            self.model.state_dict(), 
            os.path.join(self.output_dir, "models", save_path)
        )
        print(f"Saved model to {save_path}")

    def _init_data_loaders(self, dataset: GazeDataset):
        """
        :returns `Tuple[DataLoader, DataLoader]` train_loader, val_loader: `torch.DataLoader` objects for training and validation
        """
        BATCH_SIZE = 64
        np.random.seed(seed=42)

        train_dataset, test_dataset = dataset.split()

        # Shuffle after the split because subsequent images are highly correlated
        train_loader = DataLoader(train_dataset, BATCH_SIZE, shuffle=True)
        val_loader = DataLoader(test_dataset, BATCH_SIZE, shuffle=True)

        return train_loader, val_loader
    
    @staticmethod
    def from_save_file(save_path: str, dataset: GazeDataset, output_dir: str):
        model = GazePredictionNetwork()
        model.load_state_dict(torch.load(save_path))

        predictor = GazePredictor(model, dataset, output_dir)
        predictor.epoch = int(save_path.split("/")[-1][:-4])

        return predictor

def train_predictor():
    args = ArgParser().parse_args()

    # Use bfloat16 to speed up matrix computation
    torch.set_float32_matmul_precision("medium")

    # Create dataset and data loader
    env_name = "ms_pacman"
    single_run = "52_RZ_2394668_Aug-10-14-52-42" if args.debug else ""
    dataset = GazeDataset.from_atari_head_files(root_dir=f'data/Atari-HEAD/{env_name}', load_single_run=single_run)
    
    # Create the dir for saving the trained model
    output_dir = f"output/atari_head/{env_name}"
    model_dir = os.path.join(output_dir, "models")
    os.makedirs(model_dir, exist_ok=True)

    # Load an existing Atari HEAD model
    model_files = os.listdir(model_dir)
    if (args.load_model) and len(model_files) > 0:
        latest_epoch = sorted([int(file[:-4]) for file in model_files])[-1]
        save_path = os.path.join(model_dir, f"{latest_epoch}.pth")
        print(f"Loading existing gaze predictor from {save_path}")
        gaze_predictor = GazePredictor.from_save_file(save_path, dataset, output_dir)
    else:
        print("Creating new gaze model from hdfs5 weights")
        model = GazePredictionNetwork.from_h5(f"data/h5_gaze_predictors/{env_name}.hdf5")
        gaze_predictor = GazePredictor(model, dataset, output_dir)

    # Train the model
    gaze_predictor.train(n_epochs=100)
    kl_div, auc = gaze_predictor.eval()
    print(f"KL Divergence: {kl_div}, AUC: {auc}")

if __name__ == "__main__": 
    train_predictor()
