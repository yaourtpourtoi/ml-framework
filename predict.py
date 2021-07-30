import os
from glob import glob
import yaml
import gc
import hydra
from hydra.utils import to_absolute_path
from omegaconf import OmegaConf, DictConfig

import ROOT as R
import uproot
from lumin.nn.data.fold_yielder import FoldYielder

import numpy as np
from sklearn.model_selection import LeaveOneGroupOut
from mlflow.pyfunc import load_model

from utils.processing import fill_placeholders

@hydra.main(config_path="configs", config_name="predict")
def main(cfg: DictConfig) -> None:
    # fill placeholders in the cfg parameters
    input_path = to_absolute_path(fill_placeholders(cfg.input_path, {'{year}': cfg.year}))
    output_path = to_absolute_path(fill_placeholders(cfg.output_path, {'{year}': cfg.year}))
    os.makedirs(output_path, exist_ok=True)

    run_folder = to_absolute_path(f'mlruns/{cfg.mlflow_experimentID}/{cfg.mlflow_runID}/')
    input_pipe = to_absolute_path(fill_placeholders(cfg.input_pipe, {'{year}': cfg.year}))

    # extract feature and number of splits used in LeaveOneGroupOut() during the training
    with open(to_absolute_path(f'{run_folder}/params/xtrain_split_feature'), 'r') as f:
        xtrain_split_feature = f.read()
    with open(to_absolute_path(f'{run_folder}/params/n_splits'), 'r') as f:
        n_splits = int(f.read())
    print(f'Will split each data set into folds over values of ({xtrain_split_feature}) feature with number of splits ({n_splits})')

    # check that there are as many models logged as needed for retrieved n_splits
    model_idx = {int(s.split('/')[-1].split('model_')[-1]) for s in glob(f'{run_folder}/artifacts/model_*')}
    if model_idx != set(range(n_splits)):
        raise Exception(f'Indices of models in {run_folder}/artifacts ({model_idx}) doesn\'t correspond to the indices of splits used during the training ({set(range(n_splits))})')

    # load mlflow logged models for all folds
    models = {i_fold: load_model(f'{run_folder}/artifacts/model_{i_fold}') for i_fold in range(n_splits)}

    # extract names of training features from mlflow-stored model
    # note: not checking that the set of features is the same across models
    misc_features = OmegaConf.to_object(cfg.misc_features)
    train_features = []
    with open(to_absolute_path(f'{run_folder}/artifacts/model_0/MLmodel'), 'r') as f:
        model_cfg = yaml.safe_load(f)
    for s in model_cfg['signature']['inputs'].split('{')[1:]:
        train_features.append(s.split("\"")[3])

    # loop over input fold files
    for sample_name in cfg.sample_names:
        print(f'--> {sample_name}')
        print(f"        loading ...")
        input_filename = fill_placeholders(cfg.input_filename_template, {'{sample_name}': sample_name, '{year}': cfg.year})

        # extract DataFrame from fold file
        fy = FoldYielder(f'{input_path}/{input_filename}', input_pipe=input_pipe)
        df = fy.get_df(inc_inputs=True, deprocess=False, verbose=False, suppress_warn=True)
        for f in misc_features: # add misc features
            df[f] = fy.get_column(f)
        df['fold_id'] = df[xtrain_split_feature] % n_splits

        # split into folds and get predictions for each with corresponding model
        logo = LeaveOneGroupOut()
        y_proba, y_pred_class, y_pred_class_proba = [],[],[]
        misc_feature_values = {misc_feature: [] for misc_feature in misc_features}
        for i_fold, (_, pred_idx) in enumerate(logo.split(df, groups=df['fold_id'])):
            df_fold = df.iloc[pred_idx]

            # check that `i_fold` is the same as fold ID corresponding to each fold split
            fold_idx = set(df_fold['fold_id'])
            assert len(fold_idx)==1 and i_fold in fold_idx

            # make predictions
            print(f"        predicting fold {i_fold}...")
            y_proba = models[i_fold].predict(df_fold[train_features])
            y_pred_class.append(np.argmax(y_proba, axis=-1).astype(np.int32))
            y_pred_class_proba.append(np.max(y_proba, axis=-1).astype(np.float32))
            [a.append(df_fold[f].to_numpy()) for f, a in misc_feature_values.items()]

        y_proba = np.concatenate(y_proba)
        y_pred_class = np.concatenate(y_pred_class)
        y_pred_class_proba = np.concatenate(y_pred_class_proba)
        misc_feature_values = {f: np.concatenate(a) for f,a in misc_feature_values.items()}

        # store predictions in RDataFrame and snapshot it into output ROOT file
        print(f"        storing to output file ...")
        output_filename = fill_placeholders(cfg.output_filename_template, {'{sample_name}': sample_name, '{year}': cfg.year})
        if os.path.exists(f'{output_path}/{output_filename}'):
            os.system(f'rm {output_path}/{output_filename}')
        R_df = R.RDF.MakeNumpyDataFrame({'pred_class': y_pred_class,
                                         'pred_class_proba': y_pred_class_proba,
                                          **misc_feature_values
                                         })
        R_df.Snapshot(cfg.output_tree_name, f'{output_path}/{output_filename}')
        del(df, R_df); gc.collect()
if __name__ == '__main__':
    main()
