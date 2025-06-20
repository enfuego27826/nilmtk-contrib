from collections import OrderedDict
import numpy as np
import pandas as pd
from nilmtk.disaggregate import Disaggregator
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader, TensorDataset
from sklearn.model_selection import train_test_split
from tqdm import tqdm
import random
import os

# Set random seeds for reproducibility
random.seed(10)
np.random.seed(10)
torch.manual_seed(10)
if torch.cuda.is_available():
    torch.cuda.manual_seed(10)
    torch.cuda.manual_seed_all(10)

# Set device
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

class SequenceLengthError(Exception):
    pass

class ApplianceNotFoundError(Exception):
    pass

class RNNModel(nn.Module):
    def __init__(self, sequence_length):
        super(RNNModel, self).__init__()
        self.sequence_length = sequence_length
        
        # 1D Conv layer
        self.conv1d = nn.Conv1d(
            in_channels=1, 
            out_channels=16, 
            kernel_size=4, 
            stride=1, 
            padding=2  # padding='same' equivalent for kernel_size=4
        )
        
        # Bidirectional LSTMs
        self.lstm1 = nn.LSTM(
            input_size=16,
            hidden_size=128,
            num_layers=1,
            batch_first=True,
            bidirectional=True
        )
        
        self.lstm2 = nn.LSTM(
            input_size=256,  # 128 * 2 (bidirectional)
            hidden_size=256,
            num_layers=1,
            batch_first=True,
            bidirectional=True
        )
        
        # Fully Connected Layers
        self.fc1 = nn.Linear(512, 128)  # 256 * 2 (bidirectional)
        self.fc2 = nn.Linear(128, 1)
        
        # Dropout for regularization
        self.dropout = nn.Dropout(0.1)
    
    def forward(self, x):
        # Input shape: (batch_size, sequence_length, 1)
        # Conv1D expects: (batch_size, channels, sequence_length)
        x = x.permute(0, 2, 1)  # (batch_size, 1, sequence_length)
        
        # 1D Convolution
        x = self.conv1d(x)  # (batch_size, 16, sequence_length)
        
        # Back to (batch_size, sequence_length, channels) for LSTM
        x = x.permute(0, 2, 1)  # (batch_size, sequence_length, 16)
        
        # First Bidirectional LSTM
        x, _ = self.lstm1(x)  # (batch_size, sequence_length, 256)
        x = self.dropout(x)
        
        # Second Bidirectional LSTM
        x, _ = self.lstm2(x)  # (batch_size, sequence_length, 512)
        
        # Take the last output from the sequence
        x = x[:, -1, :]  # (batch_size, 512)
        
        # Fully Connected Layers
        x = torch.tanh(self.fc1(x))  # (batch_size, 128)
        x = self.dropout(x)
        x = self.fc2(x)  # (batch_size, 1)
        
        return x

class RNN(Disaggregator):
    
    def __init__(self, params):
        """
        Parameters to be specified for the model
        """
        self.MODEL_NAME = "RNN"
        self.models = OrderedDict()
        self.file_prefix = "{}-temp-weights".format(self.MODEL_NAME.lower())
        self.chunk_wise_training = params.get('chunk_wise_training', False)
        self.sequence_length = params.get('sequence_length', 19)
        self.n_epochs = params.get('n_epochs', 10)
        self.batch_size = params.get('batch_size', 512)
        self.appliance_params = params.get('appliance_params', {})
        self.mains_mean = params.get('mains_mean', 1800)
        self.mains_std = params.get('mains_std', 600)
        self.device = device
        
        if self.sequence_length % 2 == 0:
            print("Sequence length should be odd!")
            raise SequenceLengthError
    
    def partial_fit(self, train_main, train_appliances, do_preprocessing=True, current_epoch=0, **load_kwargs):
        # If no appliance wise parameters are provided, then compute them using the first chunk
        if len(self.appliance_params) == 0:
            self.set_appliance_params(train_appliances)
        
        print("...............RNN partial_fit running...............")
        
        # Do the pre-processing, such as windowing and normalizing
        if do_preprocessing:
            print("Preprocessing data...")
            train_main, train_appliances = self.call_preprocessing(
                train_main, train_appliances, 'train')
        
        train_main = pd.concat(train_main, axis=0)
        train_main = train_main.values.reshape((-1, self.sequence_length, 1))
        
        new_train_appliances = []
        for app_name, app_df in train_appliances:
            app_df = pd.concat(app_df, axis=0)
            app_df_values = app_df.values.reshape((-1, 1))
            new_train_appliances.append((app_name, app_df_values))
        train_appliances = new_train_appliances
        
        print(f"Training data shape: {train_main.shape}")
        
        # Progress bar for appliances
        appliance_progress = tqdm(train_appliances, desc="Training appliances", unit="appliance")
        
        for appliance_name, power in appliance_progress:
            appliance_progress.set_postfix({"Current": appliance_name})
            
            # Check if the appliance was already trained. If not then create a new model for it
            if appliance_name not in self.models:
                print(f"\nFirst model training for {appliance_name}")
                self.models[appliance_name] = self.return_network()
            # Retrain the particular appliance
            else:
                print(f"\nStarted Retraining model for {appliance_name}")
            
            model = self.models[appliance_name]
            if train_main.size > 0:
                # Sometimes chunks can be empty after dropping NANS
                if len(train_main) > 10:
                    # Convert to PyTorch tensors
                    train_x = torch.FloatTensor(train_main).to(self.device)
                    train_y = torch.FloatTensor(power).to(self.device)
                    
                    # Split data for validation
                    train_x_split, val_x_split, train_y_split, val_y_split = train_test_split(
                        train_x.cpu().numpy(), train_y.cpu().numpy(), 
                        test_size=0.15, random_state=42
                    )
                    
                    # Convert back to tensors
                    train_x_split = torch.FloatTensor(train_x_split).to(self.device)
                    val_x_split = torch.FloatTensor(val_x_split).to(self.device)
                    train_y_split = torch.FloatTensor(train_y_split).to(self.device)
                    val_y_split = torch.FloatTensor(val_y_split).to(self.device)
                    
                    # Create DataLoaders
                    train_dataset = TensorDataset(train_x_split, train_y_split)
                    val_dataset = TensorDataset(val_x_split, val_y_split)
                    train_loader = DataLoader(train_dataset, batch_size=self.batch_size, shuffle=True)
                    val_loader = DataLoader(val_dataset, batch_size=self.batch_size, shuffle=False)
                    
                    # Training loop
                    self.train_model(model, train_loader, val_loader, appliance_name, current_epoch)
    
    def train_model(self, model, train_loader, val_loader, appliance_name, current_epoch):
        optimizer = optim.Adam(model.parameters(), lr=0.001)
        criterion = nn.MSELoss()
        
        best_val_loss = float('inf')
        best_model_state = None
        
        # Progress bar for epochs
        epoch_progress = tqdm(range(self.n_epochs), desc=f"Training {appliance_name}", unit="epoch")
        
        for epoch in epoch_progress:
            # Training phase
            model.train()
            train_loss = 0.0
            
            # Progress bar for training batches
            train_batch_progress = tqdm(train_loader, desc=f"Epoch {epoch+1} Training", 
                                      leave=False, unit="batch")
            
            for batch_x, batch_y in train_batch_progress:
                optimizer.zero_grad()
                
                outputs = model(batch_x)
                loss = criterion(outputs.squeeze(), batch_y.squeeze())
                
                loss.backward()
                optimizer.step()
                
                train_loss += loss.item()
                train_batch_progress.set_postfix({"Loss": f"{loss.item():.4f}"})
            
            # Validation phase
            model.eval()
            val_loss = 0.0
            
            # Progress bar for validation batches
            val_batch_progress = tqdm(val_loader, desc=f"Epoch {epoch+1} Validation", 
                                    leave=False, unit="batch")
            
            with torch.no_grad():
                for batch_x, batch_y in val_batch_progress:
                    outputs = model(batch_x)
                    loss = criterion(outputs.squeeze(), batch_y.squeeze())
                    val_loss += loss.item()
                    val_batch_progress.set_postfix({"Loss": f"{loss.item():.4f}"})
            
            train_loss /= len(train_loader)
            val_loss /= len(val_loader)
            
            # Update epoch progress bar
            epoch_progress.set_postfix({
                "Train Loss": f"{train_loss:.4f}",
                "Val Loss": f"{val_loss:.4f}",
                "Best": f"{best_val_loss:.4f}"
            })
            
            # Save best model
            if val_loss < best_val_loss:
                best_val_loss = val_loss
                best_model_state = model.state_dict().copy()
                epoch_progress.write(f'New best model saved with val_loss: {val_loss:.4f}')
                
                # Save model weights (equivalent to ModelCheckpoint)
                filepath = f"{self.file_prefix}-{appliance_name.replace(' ', '_')}-epoch{current_epoch}.pth"
                torch.save(best_model_state, filepath)
        
        # Load best model
        if best_model_state is not None:
            model.load_state_dict(best_model_state)
            print(f"\nLoaded best model for {appliance_name} with validation loss: {best_val_loss:.4f}")
    
    def disaggregate_chunk(self, test_main_list, model=None, do_preprocessing=True):
        if model is not None:
            self.models = model
        
        # Preprocess the test mains such as windowing and normalizing
        if do_preprocessing:
            print("Preprocessing test data...")
            test_main_list = self.call_preprocessing(
                test_main_list, submeters_lst=None, method='test')
        
        test_predictions = []
        
        # Progress bar for test chunks
        chunk_progress = tqdm(test_main_list, desc="Processing test chunks", unit="chunk")
        
        for test_main in chunk_progress:
            test_main = test_main.values
            test_main = test_main.reshape((-1, self.sequence_length, 1))
            test_main_tensor = torch.FloatTensor(test_main).to(self.device)
            
            disggregation_dict = {}
            
            # Progress bar for appliances in each chunk
            appliance_progress = tqdm(self.models.items(), desc="Disaggregating appliances", 
                                    leave=False, unit="appliance")
            
            for appliance, model in appliance_progress:
                appliance_progress.set_postfix({"Current": appliance})
                
                model.eval()
                
                # Create DataLoader for batched prediction
                test_dataset = TensorDataset(test_main_tensor)
                test_loader = DataLoader(test_dataset, batch_size=self.batch_size, shuffle=False)
                
                predictions = []
                
                # Progress bar for prediction batches
                pred_progress = tqdm(test_loader, desc=f"Predicting {appliance}", 
                                   leave=False, unit="batch")
                
                with torch.no_grad():
                    for batch_x, in pred_progress:
                        batch_pred = model(batch_x)
                        predictions.append(batch_pred.cpu().numpy())
                
                prediction = np.concatenate(predictions, axis=0)
                
                # Denormalize predictions
                prediction = (self.appliance_params[appliance]['mean'] + 
                            prediction * self.appliance_params[appliance]['std'])
                valid_predictions = prediction.flatten()
                valid_predictions = np.where(valid_predictions > 0, valid_predictions, 0)
                df = pd.Series(valid_predictions)
                disggregation_dict[appliance] = df
            
            results = pd.DataFrame(disggregation_dict, dtype='float32')
            test_predictions.append(results)
        
        return test_predictions
    
    def return_network(self):
        """Creates the RNN module described in the paper"""
        model = RNNModel(self.sequence_length).to(self.device)
        return model
    
    def call_preprocessing(self, mains_lst, submeters_lst, method):
        if method == 'train':
            mains_df_list = []
            
            # Progress bar for mains preprocessing
            mains_progress = tqdm(mains_lst, desc="Processing mains data", unit="chunk")
            
            for mains in mains_progress:
                new_mains = mains.values.flatten()
                n = self.sequence_length
                units_to_pad = n // 2
                new_mains = np.pad(new_mains, (units_to_pad, units_to_pad), 'constant', constant_values=(0, 0))
                new_mains = np.array([new_mains[i:i + n] for i in range(len(new_mains) - n + 1)])
                new_mains = (new_mains - self.mains_mean) / self.mains_std
                mains_df_list.append(pd.DataFrame(new_mains))
            
            appliance_list = []
            
            # Progress bar for appliances preprocessing
            app_progress = tqdm(submeters_lst, desc="Processing appliance data", unit="appliance")
            
            for app_index, (app_name, app_df_list) in enumerate(app_progress):
                app_progress.set_postfix({"Current": app_name})
                
                if app_name in self.appliance_params:
                    app_mean = self.appliance_params[app_name]['mean']
                    app_std = self.appliance_params[app_name]['std']
                else:
                    print("Parameters for ", app_name, " were not found!")
                    raise ApplianceNotFoundError()
                
                processed_appliance_dfs = []
                
                # Progress bar for appliance dataframes
                df_progress = tqdm(app_df_list, desc=f"Processing {app_name} chunks", 
                                 leave=False, unit="chunk")
                
                for app_df in df_progress:
                    new_app_readings = app_df.values.reshape((-1, 1))
                    # This is for choosing windows
                    new_app_readings = (new_app_readings - app_mean) / app_std
                    # Return as a list of dataframe
                    processed_appliance_dfs.append(pd.DataFrame(new_app_readings))
                
                appliance_list.append((app_name, processed_appliance_dfs))
            
            return mains_df_list, appliance_list
        
        else:
            mains_df_list = []
            
            # Progress bar for test mains preprocessing
            mains_progress = tqdm(mains_lst, desc="Processing test mains data", unit="chunk")
            
            for mains in mains_progress:
                new_mains = mains.values.flatten()
                n = self.sequence_length
                units_to_pad = n // 2
                new_mains = np.pad(new_mains, (units_to_pad, units_to_pad), 'constant', constant_values=(0, 0))
                new_mains = np.array([new_mains[i:i + n] for i in range(len(new_mains) - n + 1)])
                new_mains = (new_mains - self.mains_mean) / self.mains_std
                mains_df_list.append(pd.DataFrame(new_mains))
            
            return mains_df_list
    
    def set_appliance_params(self, train_appliances):
        print("Setting appliance parameters...")
        
        # Progress bar for setting appliance parameters
        param_progress = tqdm(train_appliances, desc="Computing appliance stats", unit="appliance")
        
        # Find the parameters using the first
        for (app_name, df_list) in param_progress:
            param_progress.set_postfix({"Current": app_name})
            
            l = np.array(pd.concat(df_list, axis=0))
            app_mean = np.mean(l)
            app_std = np.std(l)
            if app_std < 1:
                app_std = 100
            self.appliance_params.update({app_name: {'mean': app_mean, 'std': app_std}})
        
        print(self.appliance_params)