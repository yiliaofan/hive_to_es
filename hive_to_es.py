#! /usr/bin/env python
# -*- coding:utf-8 -*-
import codecs
import logging
import time
import sys

from impala.dbapi import connect as big_data_connection
from elasticsearch import Elasticsearch
from elasticsearch import helpers as elasticsearch_helper

from imp import reload

reload(sys)
try:
    # Python3
    import configparser as ConfigParser
except:
    # Python2
    import ConfigParser

    sys.setdefaultencoding('utf8')

"""
Created by tangqingchang on 2017-09-02
环境: python2 
python hive_to_es.py config=<配置文件路径>
"""


def get_map(param_list):
    """
    解析键值对形式的参数数组，返回dict
    :param param_list: 参数数组，如sys.argv
    :return:
    """
    param_dict = {}
    try:
        for pair in param_list:
            ls = pair.split('=')
            param_dict[ls[0]] = ls[1]
    except:
        return {}
    return param_dict


def get_list(data, f=','):
    """
    分割字符串为数组
    :param data: 字符串
    :param f: 分隔符，默认是','
    :return:
    """
    ls = data.split(f)
    return ls


logging.basicConfig(level=logging.INFO)


def log(*content):
    """
    输出日志
    :param content:
    :return:
    """
    log_content = "[{t}]".format(t=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time())))
    for c in content:
        log_content += str(c)
    logging.info(log_content)


def s2t(seconds):
    """
    秒转化为时间字符串
    :param seconds:
    :return:
    """
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return "%02d:%02d:%02d" % (h, m, s)


def get_file_content(path):
    """
    读文件
    :param path:
    :return:
    """
    file = codecs.open(path, 'r+', 'utf-8', 'ignore')
    data = file.read()
    file.close()
    return data


def run_query(sql):
    """
    执行mpala-SQL或者hiveQL获得结果
    :param sql:
    :return:
    """
    cur = big_data_conn.cursor()
    cur.execute(sql)
    des = cur.description
    res = cur.fetchall()
    res_data = []

    # 拼接成字典
    for r in res:
        d = dict()
        for i, v in enumerate(r):
            if '.' in des[i][0]:
                d[des[i][0].split('.')[1]] = v
                pass
            else:
                d[des[i][0]] = v
        res_data.append(d)
    return res_data


def add_row_number_into_sql(sql):
    """
    拼接为支持分页的SQL
    :param sql:
    :return:
    """
    ql = sql.lstrip()
    start_pos = sql.upper().find("FROM ")
    left = ql[:start_pos]
    right = ql[start_pos:]
    left = left + ",ROW_NUMBER() OVER () AS row_number "
    return "SELECT * FROM(" + left + right + ")t_paging"


def add_paging_limit_into_sql(sql, start_row, to_row):
    """
    拼接为支持分页的SQL，加入分页信息
    :param sql:
    :param start_row:
    :param to_row:
    :return:
    """
    return add_row_number_into_sql(sql) + " WHERE row_number BETWEEN " + str(start_row) + " AND " + str(to_row)


def get_config_fallback(conf, k, v, fallback):
    """
    获取不到配置信息时返回fallback
    :param conf:
    :param k:
    :param v:
    :param fallback:
    :return:
    """
    try:
        return conf.get(k, v)
    except:
        return fallback


if len(sys.argv) < 2:
    log("参数不足")
    exit(0)

params_dict = get_map(sys.argv[1:])

config = ConfigParser.ConfigParser()
config.readfp(open(params_dict['config'], mode='r+'))
es = Elasticsearch(hosts=get_list(config.get("es", "hosts")),
                   http_auth=(config.get("es", "username"),
                              config.get("es", "password")))

# TODO 引入impala渠道导数据
by = get_config_fallback(config, "es", "by", fallback="hive")
log("导数据途径：", by)
big_data_conn = big_data_connection(host=config.get(by, "host"),
                                    port=config.get(by, "port"),
                                    database=config.get(by, "database"),
                                    user=get_config_fallback(config, by, "user", fallback=""),
                                    auth_mechanism=get_config_fallback(config, by, "auth_mechanism", fallback=""),
                                    )

DEFAULT_ES_INDEX = config.get("es", "default_index")
MAX_PAGE_SIZE = int(get_config_fallback(config, "paging", "max_page_size", fallback=30000))


def run_job(job_config):
    """
     一个任务
    :return:
    """
    log("*************************", job_config['table'], "开始*************************")
    PAGE_SIZE = job_config["page_size"]
    ES_INDEX = job_config["es_index"]
    ES_TYPE = job_config["es_type"]
    COLUMN_MAPPING = job_config['column_mapping']
    OVERWRITE = job_config["overwrite"]

    if len(job_config["sql_path"]) > 0:
        SQL_PATH = job_config["sql_path"]
        log("SQL文件: ", SQL_PATH)
        try:
            USER_SQL = get_file_content(SQL_PATH).strip()
        except:
            log("读取SQL文件出错，退出")
            return
    else:
        log("无SQL文件，直接导表数据")
        # TODO 可选字段导表
        USER_SQL = "SELECT * FROM " + job_config['table']

    log("ES_INDEX: ", ES_INDEX)
    log("ES_TYPE: ", ES_TYPE)
    log("分页大小: ", PAGE_SIZE)
    log("是否全量：", OVERWRITE)
    log("字段名称映射：", COLUMN_MAPPING)
    log("原始SQL内容: ", USER_SQL)
    if not (USER_SQL.startswith("select") or USER_SQL.startswith("SELECT")):
        log("只允许SELECT语句, 退出该任务")
        return

    log(">>>>>>>>>>>>>>>初始化结束>>>>>>>>>>>>>>>")

    # 开始记录时间
    start_time = time.time()

    prepare_sql = ("SELECT COUNT(*) AS c, MIN(row_number) AS m FROM (" + add_row_number_into_sql(USER_SQL) + ")t_count")
    log("Prepare SQL: ", prepare_sql)
    try:
        log("开始获取总行数和分页起始行...")
        pre_result = run_query(prepare_sql)
        total_count = int(pre_result[0]['c'])
        current_row_num = int(pre_result[0]['m'])

        if total_count == 0:
            log("数据结果为0，退出该任务")
            return
    except Exception as e:
        log("获取分页信息SQL执行失败，退出该任务：", e)
        return

    page_count = int((total_count + PAGE_SIZE - 1) / PAGE_SIZE)

    log("结果集合总数: ", total_count)
    log("分页大小: ", PAGE_SIZE)
    log("总页数: ", page_count)
    log("起始行：", current_row_num)

    # es准备
    if es.indices.exists(index=ES_INDEX) is True:
        if OVERWRITE == "true":
            log("全量添加结果集")
            # 删除type下所有数据
            es.delete_by_query(index=ES_INDEX,
                               body={"query": {"match_all": {}}},
                               doc_type=ES_TYPE,
                               params={"conflicts": "proceed"})
        else:
            log("增量添加结果集")
            pass
    else:
        es.indices.create(index=ES_INDEX)
        log("已新创建index：", ES_INDEX)

    # 开始查询
    for p in range(0, page_count):
        log("==================第%s页开始===================" % (p + 1))
        s = time.time()
        log("当前行: ", current_row_num)

        start_row = current_row_num
        to_row = current_row_num + PAGE_SIZE - 1
        log("开始行号: ", start_row)
        log("结束行号: ", to_row)

        final_sql = add_paging_limit_into_sql(USER_SQL, start_row, to_row)

        try:
            log("开始执行: ")
            log(final_sql)
            hive_result = run_query(final_sql)
        except Exception as e:
            log(">>>>>>>>>>>>>>>SQL执行失败，结束该任务：", e, ">>>>>>>>>>>>>>>>>>")
            return

        actions = []
        for r in hive_result:
            _source = dict()
            obj = dict()
            # 根据字段名称映射生成目标文档
            for k in r:
                if k == 'row_number':
                    continue
                if COLUMN_MAPPING.get(k) is not None:
                    _source[COLUMN_MAPPING.get(k)] = r[k]
                else:
                    _source[k] = r[k]
            obj['_index'] = ES_INDEX
            obj['_type'] = ES_TYPE
            obj['_source'] = _source

            actions.append(obj)

        log("开始插入结果到ES...")
        if len(actions) > 0:
            elasticsearch_helper.bulk(es, actions)
        log("插入ES结束...")
        e = time.time()
        log("该页查询时间：", s2t(e - s))
        current_row_num = current_row_num + PAGE_SIZE

    end_time = time.time()
    log("************************", job_config['table'], ": 全部结束，花费时间：", s2t(end_time - start_time),
        "************************")


result_tables = get_list(get_config_fallback(config, "table", "tables", fallback=""))
for result in result_tables:
    job_conf = dict()

    job_conf['table'] = result
    job_conf['column_mapping'] = get_map(get_list(get_config_fallback(config, result, "column_mapping", fallback="")))
    job_conf['es_index'] = get_config_fallback(config, result, "es_index", fallback=DEFAULT_ES_INDEX)
    job_conf['es_type'] = get_config_fallback(config, result, "es_type", fallback=result)

    job_conf['page_size'] = min(int(get_config_fallback(config, result, "page_size", fallback=MAX_PAGE_SIZE)),
                                MAX_PAGE_SIZE)
    # 默认全量导表
    job_conf['overwrite'] = get_config_fallback(config, result, "overwrite", fallback="true")

    job_conf['sql_path'] = get_config_fallback(config, result, "sql_path", fallback="")
    try:
        run_job(job_conf)
    except Exception as e:
        log(result, "执行job出错：", job_conf, ": ", e)

big_data_conn.close()
