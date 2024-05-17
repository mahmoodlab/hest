from operator import itemgetter
from pathlib import Path
from typing import Dict, List, Union
from matplotlib import pyplot as plt
from sklearn.preprocessing import RobustScaler
import torch
import argparse
import os
import h5py 
import json
import sklearn
import shutil
import pandas as pd
import sys
import numpy as np
import pdb
from tqdm import tqdm, trange
from torchvision import transforms
import torch.multiprocessing
import yaml
from loguru import logger

torch.multiprocessing.set_sharing_strategy('file_system')

sys.path.append('../')

from hest.HESTData import HESTData
from hest.bench.data_modules.st_dataset import H5TileDataset, load_adata
from hest.bench.utils.transform_utils import get_eval_transforms
from hest.bench.utils.utils import (get_current_time, merge_dict)
from hest.bench.utils.file_utils import save_pkl, load_pkl, save_hdf5, read_assets_from_h5
from hest.bench.training.trainer import train_test_reg

from hest.bench.cpath_model_zoo.builder import get_encoder
from hest.bench.cpath_model_zoo.model_registry import _MODEL_CONFIGS



# Generic training settings
parser = argparse.ArgumentParser(description='Configurations for linear probing')
### optimizer settings ###
parser.add_argument('--seed', type=int, default=1,
                    help='random seed for reproducible experiment (default: 1)')
parser.add_argument('--overwrite', action='store_true', default=False,
                    help='overwrite existing results')
parser.add_argument('--source_dataroot', type=str, default='/mnt/ssd/paul/ST-histology-loader/bench_data', help='root directory containing all the datasets')
parser.add_argument('--embed_dataroot', type=str)
parser.add_argument('--results_dir', type=str)
parser.add_argument('--exp_code', type=str, default=None)

### specify encoder settings ### 
parser.add_argument('--precision', type=str, default='fp32', help='Precision (fp32 or fp16)')
parser.add_argument('--img_resize', type=int, default=224, 
                    help='Image resizing (-1 to not resize)')
parser.add_argument('--batch_size', type=int, default=128, help='Batch size')
parser.add_argument('--num_workers', type=int, default=1, help='Number of workers for dataloader')

### specify dataset settings ###
parser.add_argument('--gene_list', type=str, default=None)
parser.add_argument('--alpha', type=float, default=None)
parser.add_argument('--kfold', action='store_true', default=False)
parser.add_argument('--benchmark_encoders', action='store_true', default=False)
parser.add_argument('--normalize', type=bool, default=True)
parser.add_argument('--encoders', nargs='+', help='All the encoders to benchmark', default=[])
parser.add_argument('--datasets', nargs='+', help='Datasets from source_dataroot to use during benchmark', default=['*'])
parser.add_argument('--config', type=str, help='Path to a benchmark config file, arguments provided in the config file will overwrite the command line args', default=None)

class LazyEncoder:
    
    def __init__(self, name, transforms=None, model=None, ):
        self.name = name
        self.model = model
        self.transforms = transforms
        
    def get_model(self, device):
        if self.model is not None:
            return self.model, self.transforms
        else:
            encoder, img_transforms, _ = load_encoder(self.name, device)
            return encoder, img_transforms
            


def benchmark_grid(fn, args, device, encoders: List[LazyEncoder], datasets: List[str], save_dir, precision):
    "execute fn for each encoders and datasets and dump the results in a nested directory structure"
    dataset_perfs = []
    for dataset in datasets:
        source_dataroot = os.path.join(args.source_dataroot, dataset)
        enc_perfs = []
        for enc in encoders:
            exp_save_dir = os.path.join(save_dir, dataset, enc.name)
            os.makedirs(exp_save_dir, exist_ok=True)
            enc_results = fn(args, exp_save_dir, enc, dataset, device, precision, source_dataroot)
            
            enc_perfs.append({
                'encoder_name': enc.name,
                'pearson_mean': enc_results['pearson_mean'], 
                'pearson_std': enc_results['pearson_std'], 
            })
            
        with open(os.path.join(save_dir, dataset, 'enc_results.json'), 'w') as f:
            #enc_perf = sorted(enc_perf.items(), key=lambda x: x[1]['pearson_mean'])
            enc_perfs = sorted(enc_perfs, key=itemgetter('pearson_mean'), reverse=True)
            json.dump({'results': enc_perfs}, f, sort_keys=True, indent=4)
            

        dataset_perfs.append({
            'dataset_name': dataset,
            'results': enc_perfs
        })
        
    with open(os.path.join(save_dir, 'dataset_results.json'), 'w') as f:
        json.dump({'results': dataset_perfs}, f, sort_keys=True, indent=4)

            


def post_collate_fn(batch):
    """
    Post collate function to clean up batch
    """
    if batch["imgs"].dim() == 5:
        assert batch["imgs"].size(0) == 1
        batch["imgs"] = batch["imgs"].squeeze(0)
    if batch["coords"].dim() == 3:
        assert batch["coords"].size(0) == 1
        batch["coords"] = batch["coords"].squeeze(0)
    return batch


def embed_tiles(dataloader,
                model,
                embedding_save_path,
                device,
                precision=torch.float32):
    """
    Extract embeddings from tiles using encoder and save to h5 file
    """
    model.eval()
    for batch_idx, batch in tqdm(enumerate(dataloader), total=len(dataloader)):
        batch = post_collate_fn(batch)
        imgs = batch['imgs'].to(device)    
        with torch.inference_mode(), torch.cuda.amp.autocast(dtype=precision):
            embeddings = model(imgs)
        if batch_idx == 0:
            mode = 'w'
        else:
            mode = 'a'
        asset_dict = {'embeddings': embeddings.cpu().numpy()}
        asset_dict.update({key: np.array(val) for key, val in batch.items() if key != 'imgs'})
        save_hdf5(embedding_save_path,
                  asset_dict=asset_dict,
                  mode=mode)
    return embedding_save_path  

def load_encoder(enc_name, device):
    # instantiate encoder model
    cur_dir = os.path.dirname(os.path.abspath(__file__))
    local_ckpt_registry = os.path.join(cur_dir, '..', 'local_ckpts.json')
    with open(local_ckpt_registry, 'r') as f:
        ckpt_registry = json.load(f)
    
    overwrite_kwargs = {}
    if enc_name in ckpt_registry:
        overwrite_kwargs.update({'checkpoint_path': ckpt_registry[enc_name]})
    encoder, img_transforms, enc_config = get_encoder(model_name = enc_name, overwrite_kwargs=overwrite_kwargs)
    logger.info(f"Encoder: {enc_config}")
    _ = encoder.eval()
    encoder.to(device)
    return encoder, img_transforms, enc_config

def predict_single_split(train_split, test_split, args, save_dir, dataset_name, lazy_enc, device, precision, source_dataroot):
    if not os.path.isfile(train_split):
        train_split = os.path.join(source_dataroot, 'splits', train_split)
    if not os.path.isfile(test_split):
        test_split = os.path.join(source_dataroot, 'splits', test_split)
    
    train_df = pd.read_csv(train_split)
    test_df = pd.read_csv(test_split)
    
    embedding_dir = os.path.join(args.embed_dataroot, dataset_name, lazy_enc.name, args.precision)
    os.makedirs(embedding_dir, exist_ok=True)
    
    # perform embedding
    logger.info(f"Embedding tiles using {lazy_enc.name} encoder")
    for split in [train_df, test_df]:
        for i in tqdm(range(len(split))):
            sample_id = split.iloc[i]['sample_id']
            tile_h5_path = os.path.join(source_dataroot, split.iloc[i]['patches_path'])
            assert os.path.isfile(tile_h5_path)
            embed_path = os.path.join(embedding_dir, f'{sample_id}.h5')
            if not os.path.isfile(embed_path) or args.overwrite:
                encoder, img_transforms = lazy_enc.get_model(device)
                #if lazy_enc.model is None:
                #    encoder, img_transforms, enc_config = load_encoder(lazy_enc, device) # delay instantiation incase we do not need to perform embedding
                tile_dataset = H5TileDataset(tile_h5_path, chunk_size=args.batch_size, img_transform=img_transforms)
                tile_dataloader = torch.utils.data.DataLoader(tile_dataset, 
                                                          batch_size=1, 
                                                          shuffle=False,
                                                          num_workers=args.num_workers)
                _ = embed_tiles(tile_dataloader, encoder, embed_path, device, precision=precision)
            else:
                logger.info(f"Skipping {sample_id} as it already exists")


    with open(os.path.join(save_dir, 'config.json'), 'w') as f:
        json.dump(vars(args), f, sort_keys=True, indent=4)

    #### Linear Probe Evaluation ####
    logger.info(f"Linear Probe Evaluation with Sklearn")
    
    # load and gather all data
    all_split_assets = {}
    
    if args.gene_list is None:
        files = os.listdir(source_dataroot)
        for f in files:
            if f.endswith('.json'):
                gene_list = f
                break
    else:
        gene_list = args.gene_list

    print(f'using gene_list {gene_list}')
    with open(os.path.join(source_dataroot, gene_list), 'r') as f:
        genes = json.load(f)['genes']
            
    for split_key, split in zip(['train', 'test'], [train_df, test_df]):
        split_assets = {}
        for i in tqdm(range(len(split))):
            sample_id = split.iloc[i]['sample_id']
            embed_path = os.path.join(embedding_dir, f'{sample_id}.h5')
            expr_path = os.path.join(source_dataroot, split.iloc[i]['expr_path'])
            assets, attrs = read_assets_from_h5(embed_path)
            barcodes = assets['barcodes'].flatten().astype(str).tolist()
            adata = load_adata(expr_path, genes=genes, barcodes=barcodes, normalize=args.normalize)
            assets['adata'] = adata.values
            split_assets = merge_dict(split_assets, assets)
        for key, val in split_assets.items(): 
            split_assets[key] = np.concatenate(val, axis=0)
        
        all_split_assets[split_key] = split_assets  
        logger.info(f"Loaded {split_key} split with {len(split_assets['embeddings'])} samples: {split_assets['embeddings'].shape}")
    
    X_train, y_train = all_split_assets['train']['embeddings'], all_split_assets['train']['adata']
    X_test, y_test = all_split_assets['test']['embeddings'], all_split_assets['test']['adata']
    
    linprobe_results, linprobe_dump = train_test_reg(X_train, X_test, y_train, y_test, random_state=args.seed, genes=genes)
    linprobe_summary = {}
    linprobe_summary.update({'n_train': len(y_train), 'n_test': len(y_test)})
    linprobe_summary.update({key: val for key, val in linprobe_results.items()})
    logger.info(linprobe_summary)
    with open(os.path.join(save_dir, f'results.json'), 'w') as f:
        json.dump(linprobe_results, f, sort_keys=True, indent=4)
    with open(os.path.join(save_dir, f'summary.json'), 'w') as f:
        json.dump(linprobe_summary, f, sort_keys=True, indent=4)
    save_pkl(os.path.join(save_dir, f'inference_dump.pkl'), linprobe_dump)
    return linprobe_results


def merge_fold_results(arr):
    aggr_dict = {}
    for dict in arr:
        for item in dict['pearson_corrs']:
            gene_name = item['name']
            correlation = item['pearson_corr']
            aggr_dict[gene_name] = aggr_dict.get(gene_name, []) + [correlation]
    
    aggr_results = []
    all_corrs = []
    for key, value in aggr_dict.items():
        aggr_results.append({
            "name": key,
            "pearson_corrs": value,
            "mean": np.mean(value),
            "std": np.std(value)
        })
        all_corrs += value
        
    mean_per_split = [d['pearson_mean'] for d in arr]    
        
    return {"pearson_corrs": aggr_results, "pearson_mean": np.mean(mean_per_split), "pearson_std": np.std(mean_per_split), "mean_per_split": mean_per_split}


def benchmark_encoder(encoder: torch.nn.Module, enc_transf, config_path: str) -> dict:
    
    args = parser.parse_args()

            
    args.config = config_path
    
    
    benchmark(args, encoder=encoder, enc_transf=enc_transf)
        
        
def predict_folds(args, exp_save_dir, enc, dataset_name, device, precision, source_dataroot):
    split_dir = os.path.join(source_dataroot, 'splits')
    splits = os.listdir(split_dir)
    n_splits = len(splits) // 2
    
    libprobe_results_arr = []
    for i in range(n_splits):
        train_split = os.path.join(split_dir, f'train_{i}.csv')
        test_split = os.path.join(split_dir, f'test_{i}.csv')
        kfold_save_dir = os.path.join(exp_save_dir, f'split{i}')
        os.makedirs(kfold_save_dir, exist_ok=True)
        linprobe_results = predict_single_split(train_split, test_split, args, kfold_save_dir, dataset_name, lazy_enc=enc, device=device, precision=precision, source_dataroot=source_dataroot)
        libprobe_results_arr.append(linprobe_results)
        
        
    kfold_results = merge_fold_results(libprobe_results_arr)
    with open(os.path.join(exp_save_dir, f'results_kfold.json'), 'w') as f:
        p_corrs = kfold_results['pearson_corrs']
        p_corrs = sorted(p_corrs, key=itemgetter('mean'), reverse=True)
        kfold_results['pearson_corrs'] = p_corrs
        json.dump(kfold_results, f, sort_keys=True, indent=4)
        
    return kfold_results
            


def benchmark(args, encoder, enc_transf):
    
    if args.config is not None:
        with open(args.config) as stream:
            config = yaml.safe_load(stream)
            
        for key in config:
            if key in args:
                setattr(args, key, config[key])
    
    
    logger.info(f'Benchmarking on the following datasets {args.datasets}')
    
    logger.info(f'run parameters {args}')
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    precisions = {'fp16': torch.float16, 
                  'fp32': torch.float32}
    precision = precisions.get(args.precision, torch.float32)


    datasets = args.datasets
    if len(datasets) >= 1 and datasets[0] == '*':
        datasets = os.listdir(args.source_dataroot)
    
    #### Setup Save Directory ####
    save_dir = args.results_dir
    if args.exp_code is None:
        exp_code = f"run_{get_current_time()}"
    else:
        exp_code = args.exp_code + f"::{get_current_time()}"
    save_dir = os.path.join(save_dir, exp_code)
    os.makedirs(save_dir, exist_ok=True)
    
    encoders = []
    if encoder is not None:
        encoders.append(LazyEncoder('custom_encoder', transforms=enc_transf, model=encoder))
    for enc_name in args.encoders:
        encoders.append(LazyEncoder(enc_name))
        
    
    benchmark_grid(predict_folds, args, device, encoders, datasets, save_dir=save_dir, precision=precision)

    
    
    
if __name__ == '__main__':
    args = parser.parse_args()
    
    if args.config is None and (args.source_dataroot is None or args.embed_dataroot, args.results_dir):
        parser.error("if --config isn't provided, --source_dataroot, --embed_dataroot and --results_dir must be provided")
    
    benchmark(args, None, None)
    