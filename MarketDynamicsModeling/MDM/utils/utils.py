import inspect
import os
import re

import mmcv
from mmcv import Config
from mmcv.utils import Registry
from mmcv.utils import print_log
import numpy as np
import prettytable
import random
import torch
import plotly.graph_objects as go
import os.path as osp
import pickle
from scipy.stats import norm
from argparse import Namespace
from collections import OrderedDict
import matplotlib.pyplot as plt
import pandas as pd

def set_seed(random_seed):
    random.seed(random_seed)
    torch.cuda.manual_seed(random_seed)
    torch.cuda.manual_seed_all(random_seed)
    np.random.seed(random_seed)
    torch.manual_seed(random_seed)
    torch.backends.cudnn.benckmark = False
    torch.backends.cudnn.deterministic = True
    torch.set_default_dtype(torch.float32)
def print_metrics(stats):
    table = prettytable.PrettyTable()
    # table.add_row(['' for _ in range(len(stats))])
    for key, value in stats.items():
        table.add_column(key, value)
    return table

def reduce_mem_usage(df, verbose=True):
    numerics = ['int16', 'int32', 'int64', 'float16', 'float32', 'float64']
    start_mem = df.memory_usage().sum() / 1024**2
    for col in df.columns:
        col_type = df[col].dtypes
        if col_type in numerics:
            c_min = df[col].min()
            c_max = df[col].max()
            if str(col_type)[:3] == 'int':
                if c_min > np.iinfo(np.int8).min and c_max < np.iinfo(np.int8).max:
                    df[col] = df[col].astype(np.int8)
                elif c_min > np.iinfo(np.int16).min and c_max < np.iinfo(np.int16).max:
                    df[col] = df[col].astype(np.int16)
                elif c_min > np.iinfo(np.int32).min and c_max < np.iinfo(np.int32).max:
                    df[col] = df[col].astype(np.int32)
                elif c_min > np.iinfo(np.int64).min and c_max < np.iinfo(np.int64).max:
                    df[col] = df[col].astype(np.int64)
            else:
                if c_min > np.finfo(np.float16).min and c_max < np.finfo(np.float16).max:
                    df[col] = df[col].astype(np.float16)
                elif c_min > np.finfo(np.float32).min and c_max < np.finfo(np.float32).max:
                    df[col] = df[col].astype(np.float32)
                else:
                    df[col] = df[col].astype(np.float64)
    end_mem = df.memory_usage().sum() / 1024**2
    if verbose:
        print('Mem. usage decreased to {:5.2f} Mb ({:.1f}% reduction)'.format(end_mem, 100 * (start_mem - end_mem) / start_mem))
        df.info()
    return df


def get_attr(args, key=None, default_value=None):
    if isinstance(args, dict):
        return args[key] if key in args else default_value
    elif isinstance(args, object):
        return getattr(args, key, default_value) if key is not None else default_value


def build_from_cfg(cfg, registry, default_args=None):
    """Build a module from config dict.

    Args:
        cfg (dict): Config dict. It should at least contain the key "type".
        registry (:obj:`Registry`): The registry to search the type from.
        default_args (dict, optional): Default initialization arguments.

    Returns:
        object: The constructed object.
    """
    if not isinstance(cfg, dict):
        raise TypeError(f'cfg must be a dict, but got {type(cfg)}')
    if 'type' not in cfg:
        if default_args is None or 'type' not in default_args:
            raise KeyError(
                '`cfg` or `default_args` must contain the key "type", '
                f'but got {cfg}\n{default_args}')
    if not isinstance(registry, Registry):
        raise TypeError('registry must be an mmcv.Registry object, '
                        f'but got {type(registry)}')
    if not (isinstance(default_args, dict) or default_args is None):
        raise TypeError('default_args must be a dict or None, '
                        f'but got {type(default_args)}')
    args = cfg.copy()
    if default_args is not None:
        for name, value in default_args.items():
            args.setdefault(name, value)

    obj_type = args.pop('type')
    if isinstance(obj_type, str):
        obj_cls = registry.get(obj_type)
        if obj_cls is None:
            raise KeyError(
                f'{obj_type} is not in the {registry.name} registry')
    elif inspect.isclass(obj_type):
        obj_cls = obj_type
    else:
        raise TypeError(
            f'type must be a str or valid type, but got {type(obj_type)}')
    try:
        return obj_cls(**args)
    except Exception as e:
        # Normal TypeError does not print class name.
        raise type(e)(f'{obj_cls.__name__}: {e}')


def update_data_root(cfg, logger=None):
    """Update data root according to env FINTECH_DATASETS.

    If set env FINTECH_DATASETS, update cfg.data_root according to
    MMDET_DATASETS. Otherwise, using cfg.data_root as default.

    Args:
        cfg (mmcv.Config): The model config need to modify
        logger (logging.Logger | str | None): the way to print msg
    """
    assert isinstance(cfg, mmcv.Config), \
        f'cfg got wrong type: {type(cfg)}, expected mmcv.Config'

    if 'FINTECH_DATASETS' in os.environ:
        dst_root = os.environ['FINTECH_DATASETS']
        print_log(f'FINTECH_DATASETS has been set to be {dst_root}.'
                  f'Using {dst_root} as data root.')
    else:
        return

    assert isinstance(cfg, mmcv.Config), \
        f'cfg got wrong type: {type(cfg)}, expected mmcv.Config'

    def update(cfg, src_str, dst_str):
        for k, v in cfg.items():
            if isinstance(v, mmcv.ConfigDict):
                update(cfg[k], src_str, dst_str)
            if isinstance(v, str) and src_str in v:
                cfg[k] = v.replace(src_str, dst_str)

    update(cfg.data, cfg.data_root, dst_root)
    cfg.data_root = dst_root


def replace_cfg_vals(ori_cfg):
    """Replace the string "${key}" with the corresponding value.

    Replace the "${key}" with the value of ori_cfg.key in the config. And
    support replacing the chained ${key}. Such as, replace "${key0.key1}"
    with the value of cfg.key0.key1. Code is modified from `vars.py
    < https://github.com/microsoft/SoftTeacher/blob/main/ssod/utils/vars.py>`_  # noqa: E501

    Args:
        ori_cfg (mmcv.utils.config.Config):
            The origin config with "${key}" generated from a file.

    Returns:
        updated_cfg [mmcv.utils.config.Config]:
            The config with "${key}" replaced by the corresponding value.
    """

    def get_value(cfg, key):
        for k in key.split('.'):
            cfg = cfg[k]
        return cfg

    def replace_value(cfg):
        if isinstance(cfg, dict):
            return {key: replace_value(value) for key, value in cfg.items()}
        elif isinstance(cfg, list):
            return [replace_value(item) for item in cfg]
        elif isinstance(cfg, tuple):
            return tuple([replace_value(item) for item in cfg])
        elif isinstance(cfg, str):
            # the format of string cfg may be:
            # 1) "${key}", which will be replaced with cfg.key directly
            # 2) "xxx${key}xxx" or "xxx${key1}xxx${key2}xxx",
            # which will be replaced with the string of the cfg.key
            keys = pattern_key.findall(cfg)
            values = [get_value(ori_cfg, key[2:-1]) for key in keys]
            if len(keys) == 1 and keys[0] == cfg:
                # the format of string cfg is "${key}"
                cfg = values[0]
            else:
                for key, value in zip(keys, values):
                    # the format of string cfg is
                    # "xxx${key}xxx" or "xxx${key1}xxx${key2}xxx"
                    assert not isinstance(value, (dict, list, tuple)), \
                        f'for the format of string cfg is ' \
                        f"'xxxxx${key}xxxxx' or 'xxx${key}xxx${key}xxx', " \
                        f"the type of the value of '${key}' " \
                        f'can not be dict, list, or tuple' \
                        f'but you input {type(value)} in {cfg}'
                    cfg = cfg.replace(key, str(value))
            return cfg
        else:
            return cfg

    # the pattern of string "${key}"
    pattern_key = re.compile(r'\$\{[a-zA-Z\d_.]*\}')
    # the type of ori_cfg._cfg_dict is mmcv.utils.config.ConfigDict
    updated_cfg = Config(
        replace_value(ori_cfg._cfg_dict), filename=ori_cfg.filename)
    # replace the model with model_wrapper
    if updated_cfg.get('model_wrapper', None) is not None:
        updated_cfg.model = updated_cfg.model_wrapper
        updated_cfg.pop('model_wrapper')
    return updated_cfg

def evaluate_metrics(scores_dicts,print_info=False):
    time_scale_factor=252

    Excess_Profit_list = []
    daily_return_list = []
    tr_list = []
    mdd_list = []
    cr_list = []
    for scores_dict in scores_dicts:
        Excess_Profit_list.append(scores_dict['Excess Profit'])
        # print('scores_dict["total_assets"] ',scores_dict["total_assets"].shape,scores_dict["total_assets"][-1],scores_dict["total_assets"][0])
        tr_list.append(
            scores_dict["total_assets"][-1] / (scores_dict["total_assets"][0] + 1e-10) - 1)
        daily_return_list.append(scores_dict["daily_return"])
        mdd = max(
            (max(scores_dict["total_assets"]) - scores_dict["total_assets"])
            / (
            max(scores_dict["total_assets"])) + 1e-10
        )
        mdd_list.append(mdd)
        cr_list.append(np.sum(scores_dict["daily_return"]) / (mdd + 1e-10))
    output_dict={}
    output_dict['Excess_Profit'] = sum(Excess_Profit_list) / len(Excess_Profit_list)
    output_dict['tr'] = sum(tr_list) / len(tr_list)
    daily_return_merged = np.concatenate(daily_return_list, axis=0)
    output_dict['sharpe_ratio'] = np.mean(daily_return_merged) * (time_scale_factor) ** 0.5 / (np.std(daily_return_merged) + 1e-10)
    output_dict['vol'] = np.std(daily_return_merged)

    output_dict['mdd'] = sum(mdd_list) / len(mdd_list)


    output_dict['cr'] = sum(cr_list) / len(cr_list)
    neg_ret_lst = daily_return_merged[daily_return_merged < 0]
    output_dict['sor'] = np.sum(daily_return_merged) / (np.nan_to_num(np.std(neg_ret_lst),0) + 1e-10) / (
                np.sqrt(len(daily_return_merged)) + 1e-10)
    if print_info:
        stats = OrderedDict(
            {
                "Excess Profit": ["{:04f}%".format(output_dict['Excess_Profit'])],
                "Sharp Ratio": ["{:04f}".format(output_dict['sharpe_ratio'])],
                "Volatility": ["{:04f}".format(output_dict['vol'])],
                "Max Drawdown": ["{:04f}".format(output_dict['mdd'])],
                "Calmar Ratio": ["{:04f}".format(output_dict['cr'])],
                "Sortino Ratio": ["{:04f}".format(output_dict['sor'])]
            }
        )
        print(print_info)
        table = print_metrics(stats)
        print(table)
    return output_dict

def create_radar_score_baseline(dir_name,metric_path,zero_score_id='Do_Nothing',fifty_score_id='Blind_Bid'):
    # get 0-score metrics
    # noted that for Mdd and Volatility, the lower, the better.
    # So the 0-score metric for Mdd and Volatility here is actually 100-score

    # We assume that the score of all policy range within  (-100,100)
    # Do Nonthing policy will score 0
    # the baseline policy(Blind Buy for now) should score 50(-50 if worse than Do Nothing)
    # The distribution of the score of policies is a normal distribution
    # The Do Nothing policy is 0.5 percentile and baseline policy should be the 0.75 percentile(0.675 sigma away from Do Nothing)
    # Then we can score policies based on the conversion of sigma and metric value
    metric_path_zero=metric_path + '_'+zero_score_id
    zero_scores_files = [osp.join(dir_name,filename) for filename in os.listdir(dir_name) if filename.startswith(metric_path_zero)]
    zero_scores_dicts =[]
    for file in zero_scores_files:
        with open(file, 'rb') as f:
            zero_scores_dicts.append(pickle.load(f))
    # get 50-score metrics
    metric_path_fifty=metric_path + '_'+fifty_score_id
    fifty_scores_files = [osp.join(dir_name,filename) for filename in os.listdir(dir_name) if filename.startswith(metric_path_fifty)]
    fifty_scores_dicts =[]
    for file in fifty_scores_files:
        with open(file, 'rb') as f:
            fifty_scores_dicts.append(pickle.load(f))
    # We only assume the daily return follows normal distribution so to give a overall metric across multiple tests we will calculate the metrics here.
    zero_metrics=evaluate_metrics(zero_scores_dicts,print_info=zero_score_id+' policy performance summary')
    # print('fifty_scores_dicts: ',fifty_scores_dicts)
    fifty_metrics=evaluate_metrics(fifty_scores_dicts,print_info=fifty_score_id+' policy performance summary')
    # print(zero_metrics,fifty_metrics)

    metrics_sigma_dict={}
    metrics_sigma_dict['Excess_Profit']=abs(zero_metrics['Excess_Profit']-fifty_metrics['Excess_Profit'])/0.675
    metrics_sigma_dict['tr']=abs(zero_metrics['tr']-fifty_metrics['tr'])/0.675
    metrics_sigma_dict['sharpe_ratio']=abs(zero_metrics['sharpe_ratio']-fifty_metrics['sharpe_ratio'])/0.675
    # vol and mdd for Do_Nothing is score 100(3 sigma)
    metrics_sigma_dict['vol']=abs(zero_metrics['vol']-fifty_metrics['vol'])/(3-0.675)
    metrics_sigma_dict['mdd']=abs(zero_metrics['mdd']-fifty_metrics['mdd'])/(3-0.675)
    metrics_sigma_dict['cr']=abs(zero_metrics['cr']-fifty_metrics['cr'])/0.675
    metrics_sigma_dict['sor']=abs(zero_metrics['sor']-fifty_metrics['sor'])/0.675
    return metrics_sigma_dict,zero_metrics


def calculate_radar_score(dir_name,metric_path,agent_id,metrics_sigma_dict,zero_metrics):
    metric_path = metric_path + '_'+agent_id
    # print(metric_path)
    # print(os.listdir(dir_name))
    test_scores_files = [osp.join(dir_name,filename) for filename in os.listdir(dir_name) if filename.startswith(metric_path)]
    test_scores_dicts = []
    for file in test_scores_files:
        with open(file, 'rb') as f:
            test_scores_dicts.append(pickle.load(f))
    # print('test_scores_dicts:',test_scores_dicts)
    test_metrics=evaluate_metrics(test_scores_dicts,print_info='Tested '+agent_id+' policy performance summary')
    #turn metrics to sigma
    profit_metric_names=['Excess_Profit','tr','sharpe_ratio','cr','sor']
    risk_metric_names = ['vol', 'mdd']
    test_metrics_scores_dict={}
    for metric_name in profit_metric_names:
        test_metrics_scores_dict[metric_name]=norm.cdf((test_metrics[metric_name]-zero_metrics[metric_name])/metrics_sigma_dict[metric_name])*200-100
    for metric_name in risk_metric_names:
        test_metrics_scores_dict[metric_name] = norm.cdf(
           3-(test_metrics[metric_name] - zero_metrics[metric_name]) / metrics_sigma_dict[metric_name]) * 200-100
    test_metrics_scores_dict["Profitability"] = (test_metrics_scores_dict["tr"] + test_metrics_scores_dict["sharpe_ratio"] + test_metrics_scores_dict["cr"] + test_metrics_scores_dict["sor"]) / 4
    test_metrics_scores_dict["Risk Control"] = (test_metrics_scores_dict["mdd"] + test_metrics_scores_dict["vol"]) / 2

    test_metrics_scores_dict_for_print = OrderedDict(
        {
            "Excess Profit": ["{:02f}".format(test_metrics_scores_dict['Excess_Profit'])],
            "Sharp Ratio": ["{:02f}".format(test_metrics_scores_dict['sharpe_ratio'])],
            "Volatility": ["{:02f}".format(test_metrics_scores_dict['vol'])],
            "Max Drawdown": ["{:02f}".format(test_metrics_scores_dict['mdd'])],
            "Calmar Ratio": ["{:02f}".format(test_metrics_scores_dict['cr'])],
            "Sortino Ratio": ["{:02f}".format(test_metrics_scores_dict['sor'])]
        }
    )
    print('Tested scores are:')
    print(print_metrics(test_metrics_scores_dict_for_print))
    return test_metrics_scores_dict

def plot_radar_chart(data,plot_name,radar_save_path):
    data_list_profit=[]
    data_list_risk=[]
    for metric in ['Excess_Profit','sharpe_ratio','cr','sor']:
        data_list_profit.append(data[metric]+100)
    for metric in ['vol','mdd']:
        data_list_risk.append(data[metric]+100)
    Risk_Control=sum(data_list_risk)/len(data_list_risk)
    Profitability=sum(data_list_profit)/len(data_list_profit)
    fig = go.Figure()
    r_values = data_list_profit + data_list_risk
    theta_values = [0, 60, 120, 180, 240, 300]

    # Duplicate the first point at the end
    r_values.append(r_values[0])
    theta_values.append(theta_values[0])

    fig.add_trace(go.Scatterpolar(
        r=r_values,
        theta=theta_values,
        fill=None,
        line_color='peru',
        name='Metrics Radar'
    ))
    # print(data_list_profit+data_list_risk,Risk_Control,Profitability)
    fig.add_trace(go.Barpolar(
    r=[Profitability],
    theta=[90],
    width=[180],
    marker_color=["#E4FF87"],
    marker_line_color="black",
    marker_line_width=0.5,
    opacity=0.7,
    name='Profitability'
))
    fig.add_trace(go.Barpolar(
        r=[Risk_Control],
        theta=[270],
        width=[60],
        marker_color=['#709BFF'],
        marker_line_color="black",
        marker_line_width=0.5,
        opacity=0.7,
        name='Risk_Control'
    ))

    fig.update_layout(
        font_size=16,
        legend_font_size=22,
        template=None,
        barmode='overlay',
        polar=dict(
            radialaxis=dict(range=[0,200],visible=True, showticklabels=True, ticks=''
    ,tickvals = [0,50,100,150,200],
            ticktext = [-100,-50,0,50,100]
    ),
            angularaxis=dict(showticklabels=True, ticks='',
            tickvals=[0,60,120,180,240,300],
            ticktext=['Excess Profit', 'Sharp Ratio',
               'Calmar Ratio','Sortino Ratio']+['Volatility', 'Max Drawdown'])
        )
    )
    # ax = fig.add_subplot(111, polar=True)
    # ax.set_xticklabels(['-100','-50','0','50','100'])
    radar_save_name=osp.join(radar_save_path,plot_name).replace("\\", "/")
    fig.write_image(radar_save_name)
    # print('Radar plot printed to:', radar_save_name)
    return 0

def MRL_F2B_args_converter(args):
    output_args={}
    output_args['data_path']=args['dataset_path']
    output_args['method']='slice_and_merge'
    if args['dataset_name']=="order_excecution:BTC":
        output_args['OE_BTC']=True
    else:
        output_args['OE_BTC']=False

    if args['labeling_method']=="slope":
        # use auto zooming for slope
        output_args['slope_interval']=[0,0]
    # keep the same for the rest of parameters in the args for output_args
    for key in args.keys():
        if key not in output_args.keys():
            output_args[key]=args[key]
    return output_args

def plot(df,alg,color='darkcyan',save=False):
    x = range(len(df))
    y=(df["total assets"].values-df["total assets"].values[0])/df["total assets"].values[0]
    plt.plot(x, y*100, color, label=alg)
    plt.xlabel('Trading times',size=18)
    plt.ylabel('Total Return(%)',size=18)
    plt.grid(ls='--')
    plt.legend(loc='upper center', fancybox=True, ncol=1, fontsize='x-large',bbox_to_anchor=(0.49, 1.15,0,0))
    if save:
        plt.savefig("{}.pdf".format(alg))
    plt.show()


def plot_metric_against_baseline(total_asset,buy_and_hold,alg,task,color='darkcyan',save_dir=None,metric_name='Total asset'):
    # print('total_asset shape is:',total_asset.shape)
    # print(total_asset)

    #normalize total_asset and buy_and_hold by the first value
    # print('total_asset shape is:',total_asset.shape,total_asset)
    if buy_and_hold is not None:
        buy_and_hold = buy_and_hold / total_asset[0]
    total_asset=total_asset/total_asset[0]

    x = range(len(total_asset))
    # print('total_asset shape is:',total_asset.shape)
    # print('x shape is:',len(x))
    # set figure size
    plt.figure(figsize=(10, 6))
    y=total_asset
    plt.plot(x, y, color, label=alg)
    plt.xlabel('Trading times',size=18)
    plt.ylabel(metric_name,size=18)
    if buy_and_hold is not None:
        # print('buy and hold shape is:',buy_and_hold.shape)
        plt.plot(x, buy_and_hold, 'r', label='Buy and Hold')
    plt.grid(ls='--')
    plt.legend(fancybox=True, ncol=1)
    # set title
    plt.title(f'{metric_name} of {alg} in {task}')
    if save_dir is not None:
        plt.savefig(osp.join(save_dir,f"Visualization_{task}.png"))
    # plt.show()

def plot_log_trading_decision_on_market(market_features_dict, trading_points, alg, task, color='darkcyan', save_dir=None, metric_name='Level 0 Bid and Ask Distance'):
    # parse market_features_dict to get market_features
    market_features=list(market_features_dict.keys())
    x = range(len(market_features_dict[market_features[0]]))
    # create a pd.DataFrame to store trading logs of x rows
    trading_log = pd.DataFrame(index=x)
    # print('total_asset shape is:',total_asset.shape)
    # print('x shape is:',len(x))
    # set figure size
    plt.figure(figsize=(20, 12))
    fig, ax2 = plt.subplots()

    # plot the market_features on the first y axis and give a different color for each market_feature


    # for market_feature in market_features:
    #     y=market_features_dict[market_feature]
    #     plt.plot(x, y, color, label=market_feature)
    # plt.xlabel('Trading times',size=18)
    # plt.ylabel(metric_name,size=18)

    buy_trade_points=trading_points['buy']
    sell_trade_points=trading_points['sell']

    # plot trading points(buy_trade_points and sell_trade_points) as bars using the second y axis
    ax2.set_ylabel('Trading',size=12)
    buy_max=max(buy_trade_points.values()) if len(buy_trade_points)>0 else 0
    sell_max=max(sell_trade_points.values()) if len(sell_trade_points)>0 else 0
    scale=max(buy_max,sell_max)
    ax2.set_ylim(-1.1*scale,1.1*scale)
    ax2.set_yticks([-1.1*scale,0,1.1*scale])
    ax2.set_yticklabels(['sell','hold','buy'])
    counter=0
    # many trading points, use line to represent
    if len(buy_trade_points)+len(sell_trade_points)>10:
        # give different color for buy and sell
        # for buy_trade_point,buy_volume in buy_trade_points.items():
        if len(buy_trade_points)>0:
            ax2.bar(list(buy_trade_points.keys()),list(buy_trade_points.values()),width=1,label='buy',color='r')
        # for sell_trade_point,sell_volume in sell_trade_points.items():
        if len(sell_trade_points)>0:
            ax2.bar(list(sell_trade_points.keys()),list(sell_trade_points.values()),width=1,label='sell',color='g')
        # ax2.legend( fancybox=True, ncol=1)


    # few trading points, use annotation to represent
    else:
        for buy_trade_point,buy_volume in buy_trade_points.items():
            # log the trading decision to trading_log
            trading_log.loc[buy_trade_point,'buy']=buy_volume
            counter+=1
            # print('buy_trade_point is:',buy_trade_point,'buy_volume is:',buy_volume)
            buy_volume=np.round(buy_volume,2)
            plt.annotate('Buy '+str(buy_volume), xy=(buy_trade_point, 0), xytext=(buy_trade_point, 0.5*(-1**counter)),
                         arrowprops=dict(facecolor='red', shrink=0.05),)
        counter = 0
        for sell_trade_point,sell_volume in sell_trade_points.items():
            # log the trading decision to trading_log
            trading_log.loc[sell_trade_point, 'sell'] = sell_volume
            counter += 1
            # print('sell_trade_point is:', sell_trade_point, 'sell_volume is:', sell_volume,np.round(sell_volume,2),0.5*((-1)**counter),counter)
            sell_volume=np.round(sell_volume,2)
            # print('sell_volume is:',sell_volume)
            plt.annotate('Sell '+str(sell_volume), xy=(sell_trade_point, 0), xytext=(sell_trade_point, 0.5*((-1)**counter)),
                         arrowprops=dict(facecolor='green', shrink=0.05),)


    # give different color for buy and sell
    # for buy_trade_point,buy_volume in buy_trade_points.items():
    # if len(buy_trade_points)>0:
    #     ax2.bar(list(buy_trade_points.keys()),list(buy_trade_points.values()),width=1,label='buy',color='r')
    # # for sell_trade_point,sell_volume in sell_trade_points.items():
    # if len(sell_trade_points)>0:
    #     ax2.bar(list(sell_trade_points.keys()),-1*list(sell_trade_points.values()),width=1,label='sell',color='g')
    # ax2.legend(loc='upper center', fancybox=True, ncol=1)


    ax1 = ax2.twinx()
    for market_feature in market_features:
        # log the market_feature to trading_log
        trading_log[market_feature]=market_features_dict[market_feature]
        y=market_features_dict[market_feature]
        # ax1.plot(x, y, label=market_feature)
        # force line plot
        ax1.plot(x, y, label=market_feature,drawstyle='steps-post')
        # ax1.plot(x, y, label=market_feature)
    ax1.set_ylabel('Market', size=12)
    # ax1.legend(loc='lower right',fancybox=True, ncol=1)
    # ax1.set_ylabel(metric_name,size=12)
    ax1.grid(ls='--')
    # resize the figure so taht ax1 and ax2 can be shown completely and clearly
    plt.tight_layout()
    # leave blank space fot title
    plt.subplots_adjust(top=0.9)

    # add legend for ax1 and ax2 in one legend
    lines, labels = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax2.legend(lines + lines2, labels + labels2, loc='lower right', fancybox=True, ncol=1)


    # plot trading points as vertical arrows, buy points are red, sell points are green,add the volume of the trade on the arrow
    # for buy_trade_point,buy_volume in buy_trade_points.items():
    #     plt.annotate(f'buy {buy_volume}', xy=(buy_trade_point, 0), xytext=(buy_trade_point, 0.5),
    #                  arrowprops=dict(facecolor='red', shrink=0.05),)
    # for sell_trade_point,sell_volume in sell_trade_points.items():
    #     plt.annotate(f'sell {sell_volume}', xy=(sell_trade_point, 0), xytext=(sell_trade_point, 0.5),
    #                  arrowprops=dict(facecolor='green', shrink=0.05),)


    # set title
    plt.title(f'{metric_name} of {alg} in {task}')
    if save_dir is not None:
        plt.savefig(osp.join(save_dir,f"Visualization_{task}.png"))
        # save the trading_log
        trading_log.to_csv(osp.join(save_dir,f"trading_log_{task}.csv"))


