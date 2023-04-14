import torch
import torch.nn as nn
import torch.nn.functional as F

import numpy as np
import sys
import getopt
import time
import pickle

from train.ordinal_classification_net import OrdinalClassificationNet
from train.binary_classification_net import BinaryClassificationNet
from train.regression_net import RegressionNet
from train.training_utils import create_train_and_val_loaders, DeviceDataLoader, to_device, gen_learning_curve, seed_everything

import rainfall_prediction as rp

# TODO: really compute the weights!
def compute_weights_for_binary_classification(y):
    print(y.shape)
    weights = np.zeros_like(y)

    mask_NORAIN = np.where(y == 0)[0]
    print(mask_NORAIN.shape)

    mask_RAIN = np.where(y > 0)[0]
    print(mask_RAIN.shape)

    weights[mask_NORAIN] = 1
    weights[mask_RAIN] = 1

    print(f"# NO_RAIN records: {len(mask_NORAIN)}")
    print(f"# RAIN records: {len(mask_RAIN)}")
    return weights

def compute_weights_for_ordinal_classification(y):
    weights = np.zeros_like(y)

    mask_weak = np.logical_and(y >= 0, y < 5)
    weights[mask_weak] = 1

    mask_moderate = np.logical_and(y >= 5, y < 25)
    weights[mask_moderate] = 5

    mask_strong = np.logical_and(y >= 25, y < 50)
    weights[mask_strong] = 25

    mask_extreme = np.logical_and(y >= 50, y < 150)
    weights[mask_extreme] = 50

    return weights

def weighted_mse_loss(input, target, weight):
    return (weight * (input - target) ** 2).sum()

def train(X_train, y_train, X_val, y_val, prediction_task_id, pipeline_id):
    N_EPOCHS = 1
    PATIENCE = 1000
    LEARNING_RATE = .3e-6
    NUM_FEATURES = X_train.shape[2]
    BATCH_SIZE = 1024
    weight_decay = 1e-6
    
    # model.apply(initialize_weights)

    if prediction_task_id == rp.PredictionTask.ORDINAL_CLASSIFICATION:
        print("- Prediction task: ordinal classification.")
        train_weights = compute_weights_for_ordinal_classification(y_train)
        val_weights = compute_weights_for_ordinal_classification(y_val)
        train_weights = torch.FloatTensor(train_weights)
        val_weights = torch.FloatTensor(val_weights)
        loss = weighted_mse_loss
        NUM_CLASSES = 5
        model = OrdinalClassificationNet(in_channels=NUM_FEATURES, num_classes=NUM_CLASSES)
        print(f" - Shape of y_train before encoding: {y_train.shape}")
        y_train = rp.precipitationvalues_to_ordinalencoding(y_train)
        y_val = rp.precipitationvalues_to_ordinalencoding(y_val)
        print(f" - Shape of y_train after encoding: {y_train.shape}")
    elif prediction_task_id == rp.PredictionTask.BINARY_CLASSIFICATION:
        print("- Prediction task: binary classification.")
        train_weights = None
        val_weights = None
        # train_weights = compute_weights_for_binary_classification(y_train)
        # val_weights = compute_weights_for_binary_classification(y_val)
        # train_weights = torch.FloatTensor(train_weights)
        # val_weights = torch.FloatTensor(val_weights)
        # loss = F.cross_entropy
        loss = nn.CrossEntropyLoss()
        NUM_CLASSES = 2
        model = BinaryClassificationNet(in_channels=NUM_FEATURES, num_classes=NUM_CLASSES)
        y_train = rp.precipitationvalues_to_binaryonehotencoding(y_train)
        y_val = rp.precipitationvalues_to_binaryonehotencoding(y_val)
        print(f"- Shapes of one-hot-encoded vectors (train/val): {y_train.shape}/{y_val.shape}")
    elif prediction_task_id == rp.PredictionTask.REGRESSION:
        print("- Prediction task: regression.")
        loss = nn.MSELoss()
        global y_mean_value
        y_mean_value = np.mean(y_train)
        print(y_mean_value)
        model = RegressionNet(in_channels=NUM_FEATURES, y_mean_value=y_mean_value)

    print(model)

    print(f" - Create data loaders.")
    train_loader, val_loader = create_train_and_val_loaders(
        X_train, y_train, X_val, y_val, batch_size=BATCH_SIZE, train_weights=train_weights, val_weights=val_weights)

    optimizer = torch.optim.Adam(model.parameters(),
                                 lr=LEARNING_RATE,
                                 weight_decay=weight_decay)

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    train_loader = DeviceDataLoader(train_loader, device)
    val_loader = DeviceDataLoader(val_loader, device)
    to_device(model, device)

    print(f" - Fitting model...", end = " ")
    train_loss, val_loss = model.fit(n_epochs=N_EPOCHS,
                               optimizer=optimizer,
                               train_loader=train_loader,
                               val_loader=val_loader,
                               patience=PATIENCE,
                               criterion=loss,
                               pipeline_id=pipeline_id)
    print("Done!")

    gen_learning_curve(train_loss, val_loss, pipeline_id)

    return model


def main(argv):
    help_message = "Usage: {0} -p <pipeline_id>".format(argv[0])

    try:
        opts, args = getopt.getopt(
            argv[1:], "ht:p:r", ["help", "task=", "pipeline_id=", "reg"])
    except:
        print(help_message)
        sys.exit(2)

    # ordinal_classification = True

    prediction_task_id = None

    for opt, arg in opts:
        if opt in ("-h", "--help"):
            print(help_message)  # print the help message
            sys.exit(2)
        elif opt in ("-t", "--task"):
            prediction_task_id_str = arg
            if prediction_task_id_str == "ORDINAL_CLASSIFICATION":
                prediction_task_id = rp.PredictionTask.ORDINAL_CLASSIFICATION
            elif prediction_task_id_str == "BINARY_CLASSIFICATION":
                prediction_task_id = rp.PredictionTask.BINARY_CLASSIFICATION
        elif opt in ("-p", "--pipeline_id"):
            pipeline_id = arg

    if prediction_task_id is None:
        prediction_task_id = rp.PredictionTask.REGRESSION

    if prediction_task_id == rp.PredictionTask.ORDINAL_CLASSIFICATION:
        pipeline_id += "_OC" 
    if prediction_task_id == rp.PredictionTask.BINARY_CLASSIFICATION:
        pipeline_id += "_BC" 
    elif prediction_task_id == rp.PredictionTask.REGRESSION:
        pipeline_id += "_Reg"

    start_time = time.time()

    seed_everything()

    #
    # Load numpy arrays (stored in a pickle file) from disk
    #
    file = open("../data/datasets/" + pipeline_id + ".pickle", 'rb')
    (X_train, y_train,  # min_y_train, max_y_train,
        X_val, y_val,  # min_y_val, max_y_val,
        X_test, y_test) = pickle.load(file)

    #
    # Build model
    #
    model = train(X_train, y_train, X_val, y_val,
                  prediction_task_id, pipeline_id)

    # Load the best model
    model.load_state_dict(torch.load(
        '../models/best_' + pipeline_id + '.pt'))

    # y_test = rp.precipitationvalues_to_binaryonehotencoding(y_test)
    # print(f"- Shape of one-hot-encoded vector (y_test): {y_test.shape}")

    model.evaluate(X_test, y_test)

    print("--- %s seconds ---" % (time.time() - start_time))


# python train_model.py -p A652_N -t ORDINAL_CLASSIFICATION
# python train_model.py -p A652_N -t BINARY_CLASSIFICATION
if __name__ == "__main__":
    main(sys.argv)
