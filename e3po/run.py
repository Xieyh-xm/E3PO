# add by lcy : 顺序运行make_preprocessing.py,make_decision.py,make_evaluation.py
# support by xrs


from e3po.data import build_data

import os.path as osp
from e3po.utils import get_opt, get_logger, write_json

from make_decision import make_decision
from make_evaluation import make_evaluation

if __name__ == '__main__':
    opt = get_opt()
    # make_preprocessing.py
    get_logger().info('[preprocessing data] start')
    data = build_data(opt)
    data.process_video()
    get_logger().info('[preprocessing data] end')

    # make_decision.py
    if opt['decision_type'] is not None:  # transcoding mode下不跑该步骤
        get_logger().info('[make decision] start')
        decision_result = make_decision(opt)
        result_path = osp.join(opt['project_path'], 'result', opt['test_group'],
                               opt['video']['origin']['video_name'].split('.')[0], opt['method_name'], 'decision.json')
        write_json(decision_result, result_path)
        get_logger().info(f'[write json] path: {result_path}')
        get_logger().info('[make decision] end')

    # make_evaluation.py
    get_logger().info('[evaluation] start')
    evaluation_result = make_evaluation(opt)
    if opt['method_settings']['background']['background_flag']:
        result_path = osp.join(opt['project_path'], 'result', opt['test_group'],
                               opt['video']['origin']['video_name'].split('.')[0],
                               opt['method_name'], 'evaluation_w.json')
    else:
        result_path = osp.join(opt['project_path'], 'result', opt['test_group'],
                               opt['video']['origin']['video_name'].split('.')[0],
                               opt['method_name'], 'evaluation_wo.json')
    write_json(evaluation_result, result_path)
    get_logger().info(f'[write json] path: {result_path}')
    get_logger().info('[evaluation] end')
