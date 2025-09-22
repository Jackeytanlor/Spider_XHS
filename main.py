import json
import os
import re
import time
from loguru import logger
from apis.xhs_pc_apis import XHS_Apis
from xhs_utils.common_util import init
from xhs_utils.data_util import handle_note_info, download_note, save_to_xlsx

def _extract_note_id(note_url: str) -> str | None:
    """
    从 explore URL 中提取 note_id，去掉过期的 xsec_token（仅保留 id）
    """
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

    # 优先老结构
    items = data.get("items")
    if items is None:
        # 新结构或单条
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

    # 统一为列表
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        items = []
    return items

class Data_Spider():
    def __init__(self):
        self.xhs_apis = XHS_Apis()

    def spider_note(self, note_url: str, cookies_str: str, proxies=None, max_retry: int = 2):
        """
        爬取一个笔记的信息（更健壮：提取 note_id、去 token、兼容返回结构、失败重试）
        """
        note_info = None
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
                    # 可能是被风控/需要刷新 cookie；记录服务端 msg/code，避免 KeyError
                    srv_msg = raw.get("msg") or raw.get("message") or raw.get("error")
                    last_err = srv_msg or "empty items"
                    time.sleep(0.6 * attempt)
                    continue

                note_info = items[0]
                note_info["url"] = clean_url
                note_info = handle_note_info(note_info)
                logger.info(f"爬取笔记信息 {clean_url}: True, msg: 成功")
                return True, "成功", note_info

            except Exception as e:
                last_err = e
                time.sleep(0.6 * attempt)

        logger.info(f"爬取笔记信息 {clean_url}: False, msg: {last_err}")
        return False, last_err, None

    def spider_some_note(self, notes: list, cookies_str: str, base_path: dict, save_choice: str, excel_name: str = '', proxies=None):
        if (save_choice == 'all' or save_choice == 'excel') and excel_name == '':
            raise ValueError('excel_name 不能为空')
        note_list = []
        for note_url in notes:
            success, msg, note_info = self.spider_note(note_url, cookies_str, proxies)
            if success and note_info:
                note_list.append(note_info)
            else:
                logger.warning(f"跳过无效笔记: {note_url} | 原因: {msg}")

        # 先保存媒体（可选）
        if note_list and (save_choice == 'all' or 'media' in save_choice):
            for note_info in note_list:
                download_note(note_info, base_path['media'], save_choice)

        # 再保存 excel（仅当有数据）
        if note_list and (save_choice == 'all' or save_choice == 'excel'):
            file_path = os.path.abspath(os.path.join(base_path['excel'], f'{excel_name}.xlsx'))
            save_to_xlsx(note_list, file_path)
        elif (save_choice == 'all' or save_choice == 'excel'):
            logger.warning(f"无有效数据，不写入 Excel（避免只有表头）。excel_name={excel_name}")

    def spider_user_all_note(self, user_url: str, cookies_str: str, base_path: dict, save_choice: str, excel_name: str = '', proxies=None):
        note_list = []
        try:
            success, msg, all_note_info = self.xhs_apis.get_user_all_notes(user_url, cookies_str, proxies)
            if success and all_note_info:
                logger.info(f'用户 {user_url} 作品数量: {len(all_note_info)}')
                for simple_note_info in all_note_info:
                    # 只保留 id，避免过期 token
                    note_id = simple_note_info.get('note_id')
                    if not note_id:
                        continue
                    note_url = f"https://www.xiaohongshu.com/explore/{note_id}"
                    note_list.append(note_url)

            if save_choice in ('all', 'excel'):
                excel_name = excel_name or user_url.split('/')[-1].split('?')[0]

            self.spider_some_note(note_list, cookies_str, base_path, save_choice, excel_name, proxies)
            final_success, final_msg = True, "成功"
        except Exception as e:
            final_success, final_msg = False, e
        logger.info(f'爬取用户所有视频 {user_url}: {final_success}, msg: {final_msg}')
        return note_list, final_success, final_msg

    def spider_some_search_note(self, query: str, require_num: int, cookies_str: str, base_path: dict, save_choice: str, sort_type_choice=0, note_type=0, note_time=0, note_range=0, pos_distance=0, geo: dict = None,  excel_name: str = '', proxies=None):
        note_list = []
        try:
            success, msg, notes = self.xhs_apis.search_some_note(query, require_num, cookies_str, sort_type_choice, note_type, note_time, note_range, pos_distance, geo, proxies)
            if success and notes:
                notes = [x for x in notes if x.get('model_type') == "note"]
                logger.info(f'搜索关键词 {query} 笔记数量: {len(notes)}')
                for n in notes:
                    # 使用短链接（只带 id），不要携带会过期的 xsec_token
                    note_id = n.get('id')
                    if note_id:
                        note_list.append(f"https://www.xiaohongshu.com/explore/{note_id}")

            if save_choice in ('all', 'excel'):
                excel_name = excel_name or query

            self.spider_some_note(note_list, cookies_str, base_path, save_choice, excel_name, proxies)
            final_success, final_msg = True, "成功"
        except Exception as e:
            final_success, final_msg = False, e
        logger.info(f'搜索关键词 {query} 笔记: {final_success}, msg: {final_msg}')
        return note_list, final_success, final_msg

if __name__ == '__main__':
    cookies_str, base_path = init()
    data_spider = Data_Spider()

    # 1. 固定列表（示例）：注意去掉 token
    notes = [
        r'https://www.xiaohongshu.com/explore/683fe17f0000000023017c6a',
    ]
    data_spider.spider_some_note(notes, cookies_str, base_path, 'all', 'test')

    # 2. 用户所有笔记（自动改成短链接）
    user_url = 'https://www.xiaohongshu.com/user/profile/64c3f392000000002b009e45'
    data_spider.spider_user_all_note(user_url, cookies_str, base_path, 'all')

    # 3. 搜索（结果同样只保留 id）
    query = "南头古镇"
    data_spider.spider_some_search_note(
        query=query,
        require_num=100,
        cookies_str=cookies_str,
        base_path=base_path,
        save_choice='all',
        sort_type_choice=0,
        note_type=0,
        note_time=3,
        note_range=0,
        pos_distance=0,
        geo=None
    )
