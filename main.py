import json
import os
import re
import time
from loguru import logger
from apis.xhs_pc_apis import XHS_Apis
from xhs_utils.common_util import init
from xhs_utils.data_util import handle_note_info, save_to_xlsx

# =============== 工具函数：更稳健的返回解析 ===============

def _extract_note_id(note_url: str) -> str | None:
    """从 explore URL 中提取 note_id（去掉会过期的 xsec_token）"""
    m = re.search(r"/explore/([0-9a-zA-Z]+)", note_url)
    return m.group(1) if m else None

def _pick_items_from_resp(resp: dict) -> list:
    """
    兼容不同返回结构，把 “单个/多个” 统一为列表；没有数据则返回 []
    常见结构：
      {"data":{"items":[...]}}
      {"data":{"note":{...}}}
      {"items":[...]} / {"note":{...}}
    """
    if not isinstance(resp, dict):
        return []
    data = resp.get("data") if isinstance(resp.get("data"), (dict, list)) else resp
    if not isinstance(data, dict):
        return []

    items = data.get("items")
    if items is None:
        if "note" in data and isinstance(data["note"], dict):
            items = [data["note"]]
        elif "note_info" in data and isinstance(data["note_info"], dict):
            items = [data["note_info"]]
        elif "item" in data and isinstance(data["item"], dict):
            items = [data["item"]]
        elif "list" in data and isinstance(data["list"], list):
            items = data["list"]
        else:
            items = []

    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        items = []
    return items

def _safe_handle_note_info(note_info: dict) -> dict | None:
    """
    兼容多结构；若关键字段缺失导致异常，则返回 None（跳过该笔记）
    这里保留项目原有的标准化逻辑 handle_note_info
    """
    try:
        return handle_note_info(note_info)
    except Exception as e:
        logger.warning(f"handle_note_info 异常，跳过该笔记：{e}")
        return None

# =============== 只导出 Excel 的 Spider ===============

class Data_Spider_ExcelOnly():
    def __init__(self):
        self.xhs_apis = XHS_Apis()

    def spider_note(self, note_url: str, cookies_str: str, proxies=None, max_retry: int = 2):
        """
        拉取单条笔记详情（仅返回标准化后的 dict，不做任何媒体下载）
        """
        note_id = _extract_note_id(note_url)
        clean_url = f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else note_url.split("?")[0]

        last_err = None
        for attempt in range(1, max_retry + 1):
            try:
                success, msg, raw = self.xhs_apis.get_note_info(clean_url, cookies_str, proxies)
                if not success or not isinstance(raw, dict):
                    last_err = msg or "unknown error"
                    time.sleep(0.6 * attempt)
                    continue

                items = _pick_items_from_resp(raw)
                if not items:
                    srv_msg = raw.get("msg") or raw.get("message") or raw.get("error")
                    last_err = srv_msg or "empty items"
                    time.sleep(0.6 * attempt)
                    continue

                first = items[0] if isinstance(items, list) and len(items) > 0 else None
                if not first:
                    last_err = "items list empty"
                    time.sleep(0.6 * attempt)
                    continue

                first["url"] = clean_url
                std = _safe_handle_note_info(first)
                if std is None:
                    last_err = "structure unsupported / no data after normalize"
                    time.sleep(0.6 * attempt)
                    continue

                logger.info(f"爬取笔记信息 {clean_url}: True, msg: 成功")
                return True, "成功", std

            except Exception as e:
                last_err = e
                time.sleep(0.6 * attempt)

        logger.info(f"爬取笔记信息 {clean_url}: False, msg: {last_err}")
        return False, last_err, None

    def spider_some_note_to_excel(self, notes: list, cookies_str: str, excel_dir: str, excel_name: str, proxies=None):
        """
        拉取一组笔记 → 只写入 Excel（无媒体下载）
        """
        if not excel_name:
            raise ValueError("excel_name 不能为空")

        rows = []
        for note_url in notes:
            success, msg, note_info = self.spider_note(note_url, cookies_str, proxies)
            if success and note_info:
                rows.append(note_info)
            else:
                logger.warning(f"跳过无效笔记: {note_url} | 原因: {msg}")

        if rows:
            file_path = os.path.abspath(os.path.join(excel_dir, f"{excel_name}.xlsx"))
            save_to_xlsx(rows, file_path)
            logger.info(f"✅ 写入 {len(rows)} 条到 Excel: {file_path}")
        else:
            logger.warning(f"⚠️ 无有效数据，未写入 Excel（避免只有表头）。excel_name={excel_name}")

    def spider_user_all_note_to_excel(self, user_url: str, cookies_str: str, excel_dir: str, excel_name: str = "", proxies=None):
        """
        获取用户所有 note_id → 拉取详情 → 只写入 Excel
        """
        try:
            success, msg, all_note_info = self.xhs_apis.get_user_all_notes(user_url, cookies_str, proxies)
            note_urls = []
            if success and all_note_info:
                logger.info(f'用户 {user_url} 作品数量: {len(all_note_info)}')
                for sn in all_note_info:
                    nid = sn.get("note_id")
                    if nid:
                        note_urls.append(f"https://www.xiaohongshu.com/explore/{nid}")
            if not excel_name:
                excel_name = user_url.split("/")[-1].split("?")[0]
            self.spider_some_note_to_excel(note_urls, cookies_str, excel_dir, excel_name, proxies)
            return note_urls, True, "成功"
        except Exception as e:
            logger.info(f'爬取用户所有视频 {user_url}: False, msg: {e}')
            return [], False, e

    def spider_some_search_note_to_excel(self, query: str, require_num: int, cookies_str: str, excel_dir: str,
                                         excel_name: str = "", sort_type_choice=0, note_type=0, note_time=0,
                                         note_range=0, pos_distance=0, geo: dict = None, proxies=None):
        """
        搜索 → 仅导出 Excel
        """
        try:
            success, msg, notes = self.xhs_apis.search_some_note(
                query, require_num, cookies_str, sort_type_choice, note_type, note_time, note_range, pos_distance, geo, proxies
            )
            note_urls = []
            if success and notes:
                valid = [x for x in notes if x.get("model_type") == "note"]
                logger.info(f'搜索关键词 {query} 笔记数量: {len(valid)}')
                for n in valid:
                    nid = n.get("id")
                    if nid:
                        note_urls.append(f"https://www.xiaohongshu.com/explore/{nid}")

            if not excel_name:
                excel_name = query

            self.spider_some_note_to_excel(note_urls, cookies_str, excel_dir, excel_name, proxies)
            logger.info(f'搜索关键词 {query} 笔记: True, msg: 成功')
            return note_urls, True, "成功"
        except Exception as e:
            logger.info(f'搜索关键词 {query} 笔记: False, msg: {e}')
            return [], False, e

# =============== 入口（Excel-only） ===============

if __name__ == '__main__':
    """
    仅导出 Excel，不下载媒体。
    datas/excel_datas 下会生成对应的 .xlsx
    """
    cookies_str, base_path = init()  # base_path 内已有 'excel' 路径
    excel_dir = base_path['excel']

    spider = Data_Spider_ExcelOnly()

    # 1) 固定笔记列表 → Excel
    # notes = [
    #     r'https://www.xiaohongshu.com/explore/683fe17f0000000023017c6a',
    # ]
    # spider.spider_some_note_to_excel(notes, cookies_str, excel_dir, excel_name='test')

    # 2) 某用户所有笔记 → Excel（自动用用户ID做文件名）
    # user_url = 'https://www.xiaohongshu.com/user/profile/64c3f392000000002b009e45'
    # spider.spider_user_all_note_to_excel(user_url, cookies_str, excel_dir)

    # 3) 搜索关键词 → Excel（文件名默认用关键词）
    spider.spider_some_search_note_to_excel(
        query="南头古镇",
        require_num=100,
        cookies_str=cookies_str,
        excel_dir=excel_dir,
        excel_name="",           # 为空则使用 query 作为文件名
        sort_type_choice=0,      # 0 综合, 1 最新, 2 点赞多, 3 评论多, 4 收藏多
        note_type=0,             # 0 不限, 1 视频, 2 图文
        note_time=3,             # 0 不限, 1 一天, 2 一周, 3 半年
        note_range=0,            # 0 不限
        pos_distance=0,          # 0 不限
        geo=None,
        proxies=None
    )

