# replace.py
import re
import requests
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import BACKUP_SOURCE_URL, CHECK_TIMEOUT, MAX_WORKERS, CHANNEL_ALIAS_MAP

PLAYLIST_FILE = "playlist.m3u"


def parse_m3u(file_path_or_url, is_url=False):
    """
    解析 M3U 或 TXT 文件（本地或 URL），返回 [(频道名, URL)] 列表
    支持两种格式：
    - M3U格式：#EXTINF:-1,频道名\nURL
    - TXT格式：频道名,URL
    """
    channels = []
    content = ""

    if is_url:
        resp = requests.get(file_path_or_url, timeout=10)
        if resp.status_code != 200:
            raise Exception(f"无法获取备用源: HTTP {resp.status_code}")
        content = resp.text
    else:
        with open(file_path_or_url, 'r', encoding='utf-8') as f:
            content = f.read()

    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # ----- 解析 M3U 格式 -----
        if line.startswith('#EXTINF'):
            # 提取频道名（最后一个逗号后面的内容）
            if ',' in line:
                name = line.split(',')[-1].strip()
            else:
                name = "未知频道"
            i += 1
            if i < len(lines):
                url = lines[i].strip()
                if url and not url.startswith('#'):
                    channels.append((name, url))
            i += 1
            continue

        # ----- 解析 TXT 格式（频道名,URL）-----
        if ',' in line:
            parts = line.split(',', 1)  # 只分割第一个逗号
            if len(parts) == 2:
                name = parts[0].strip()
                url = parts[1].strip()
                if url.startswith('http'):
                    # 应用别名映射
                    mapped_name = CHANNEL_ALIAS_MAP.get(name, name)
                    channels.append((mapped_name, url))

        i += 1

    return channels


def check_url(url, timeout=CHECK_TIMEOUT):
    """检测单个 URL 是否有效"""
    try:
        # 使用 HEAD 请求，避免下载大量数据
        r = requests.head(url, timeout=timeout, allow_redirects=True)
        return r.status_code == 200
    except:
        return False


def build_backup_index(backup_url):
    """从备用源构建 {频道名: [URL1, URL2, ...]} 索引"""
    channels = parse_m3u(backup_url, is_url=True)
    index = {}
    for name, url in channels:
        if name not in index:
            index[name] = []
        index[name].append(url)
    return index


def replace_failed_channels(playlist_file, backup_index):
    """检测失效频道，从备用源替换"""
    channels = parse_m3u(playlist_file, is_url=False)
    print(f"📺 基准列表共有 {len(channels)} 个频道")

    # ----- 并发检测所有频道 -----
    results = {}  # {(name, url): is_valid}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(check_url, url): (name, url) for name, url in channels}
        for future in as_completed(futures):
            name, url = futures[future]
            is_valid = future.result()
            results[(name, url)] = is_valid
            status = "✅" if is_valid else "❌"
            print(f"{status} {name}: {url}")

    # ----- 读取原文件，逐行处理 -----
    with open(playlist_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    new_lines = []
    i = 0
    replaced_count = 0
    failed_count = 0

    while i < len(lines):
        line = lines[i]
        if line.startswith('#EXTINF'):
            new_lines.append(line)
            i += 1

            if i < len(lines):
                url_line = lines[i].strip()
                current_url = url_line

                # 查找当前 URL 对应的频道名
                current_name = None
                for (name, url), is_valid in results.items():
                    if url == current_url:
                        current_name = name
                        break

                # 如果该 URL 失效，尝试替换
                if current_name and not results.get((current_name, current_url), True):
                    # 尝试从备用源中查找同名频道
                    if current_name in backup_index and backup_index[current_name]:
                        new_url = backup_index[current_name][0]
                        new_lines.append(new_url + '\n')
                        replaced_count += 1
                        print(f"🔄 替换 {current_name}: {current_url} → {new_url}")
                    else:
                        # 备用源中也没有，保留原 URL 并注释标记
                        new_lines.append(url_line + '  # 已失效，无替代\n')
                        failed_count += 1
                        print(f"⚠️ {current_name}: 备用源中无替代源")
                else:
                    new_lines.append(url_line + '\n')
        else:
            new_lines.append(line)
            i += 1

    # ----- 写回文件 -----
    with open(playlist_file, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)

    print(f"✅ 完成: 替换 {replaced_count} 个，{failed_count} 个无替代源")


def main():
    print("📡 开始检测并替换失效源...")

    # 从备用源构建索引
    try:
        backup_index = build_backup_index(BACKUP_SOURCE_URL)
        total_urls = sum(len(v) for v in backup_index.values())
        print(f"📦 备用源共有 {len(backup_index)} 个频道，{total_urls} 条 URL")
    except Exception as e:
        print(f"❌ 获取备用源失败: {e}")
        return

    # 执行替换
    replace_failed_channels(PLAYLIST_FILE, backup_index)


if __name__ == "__main__":
    main()
