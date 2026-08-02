# replace.py
import re
import requests
import os
import subprocess
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

# ========== 尝试导入 config.py 中的配置，若不存在则使用默认值 ==========
try:
    from config import (
        BACKUP_SOURCE_URL,
        CHECK_TIMEOUT,
        MAX_WORKERS,
        CHANNEL_ALIAS_MAP,
        MIN_WIDTH,
        MIN_HEIGHT,
        MIN_BITRATE,
        ENABLE_QUALITY_CHECK,
    )
except ImportError:
    # 默认配置
    BACKUP_SOURCE_URL = "https://gh-proxy.com/https://raw.githubusercontent.com/kingmax1688/iptv/refs/heads/main/itvlist.txt"
    CHECK_TIMEOUT = 3
    MAX_WORKERS = 20
    CHANNEL_ALIAS_MAP = {}
    MIN_WIDTH = 1920
    MIN_HEIGHT = 1080
    MIN_BITRATE = 2000          # kbps
    ENABLE_QUALITY_CHECK = True

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
            parts = line.split(',', 1)
            if len(parts) == 2:
                name = parts[0].strip()
                url = parts[1].strip()
                if url.startswith('http'):
                    mapped_name = CHANNEL_ALIAS_MAP.get(name, name)
                    channels.append((mapped_name, url))

        i += 1

    return channels


def check_url(url, timeout=CHECK_TIMEOUT):
    """检测单个 URL 是否有效（HEAD请求）"""
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True)
        return r.status_code == 200
    except:
        return False


def get_stream_info(url):
    """
    使用 ffprobe 获取流媒体信息，返回 (width, height, bitrate_kbps)
    如果失败则返回 (None, None, None)
    """
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,bit_rate",
            "-of", "json",
            url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return None, None, None

        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if not streams:
            return None, None, None

        stream = streams[0]
        width = stream.get("width")
        height = stream.get("height")
        bit_rate = stream.get("bit_rate")
        bitrate_kbps = int(bit_rate) // 1000 if bit_rate else None
        return width, height, bitrate_kbps
    except Exception as e:
        print(f"   ⚠️ ffprobe 检测失败 ({url[:60]}...): {e}")
        return None, None, None


def is_quality_acceptable(url):
    """检测 URL 是否满足分辨率与码率阈值"""
    if not ENABLE_QUALITY_CHECK:
        return True

    width, height, bitrate = get_stream_info(url)
    if width is None:
        print(f"   ❌ 无法获取视频信息，跳过候选源")
        return False

    width_ok = width >= MIN_WIDTH
    height_ok = height >= MIN_HEIGHT
    bitrate_ok = True if bitrate is None else bitrate >= MIN_BITRATE

    if width_ok and height_ok and bitrate_ok:
        return True
    else:
        print(f"   ❌ 质量不达标: {width}x{height} {bitrate}kbps (要求 {MIN_WIDTH}x{MIN_HEIGHT} {MIN_BITRATE}kbps)")
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
    """检测失效频道，从备用源替换，并过滤质量"""
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
                    if current_name in backup_index and backup_index[current_name]:
                        found = False
                        for candidate_url in backup_index[current_name]:
                            print(f"   🔍 检查候选源: {candidate_url[:80]}...")
                            if is_quality_acceptable(candidate_url):
                                new_lines.append(candidate_url + '\n')
                                replaced_count += 1
                                print(f"🔄 替换 {current_name}: {current_url} → {candidate_url}")
                                found = True
                                break
                            else:
                                print(f"   ⏭️ 跳过质量不达标的候选源")
                        if not found:
                            new_lines.append(url_line + '  # 已失效，备用源质量不达标\n')
                            failed_count += 1
                            print(f"⚠️ {current_name}: 备用源中所有候选质量均不达标")
                    else:
                        new_lines.append(url_line + '  # 已失效，备用源无此频道\n')
                        failed_count += 1
                        print(f"⚠️ {current_name}: 备用源中无此频道")
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
