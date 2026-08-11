# replace.py
import re
import requests
import os
import subprocess
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

# ========== 尝试导入 config.py 中的配置 ==========
try:
    from config import (
        BACKUP_SOURCES,              # 新增：多源列表
        CHECK_TIMEOUT,
        MAX_WORKERS,
        CHANNEL_ALIAS_MAP,
        MIN_WIDTH,
        MIN_HEIGHT,
        MIN_BITRATE,
        ENABLE_QUALITY_CHECK,
    )
except ImportError:
    # 向后兼容：如果 config.py 不存在或没有 BACKUP_SOURCES，使用单个备用源
    BACKUP_SOURCES = None
    try:
        from config import BACKUP_SOURCE_URL
    except ImportError:
        BACKUP_SOURCE_URL = "https://gh-proxy.com/https://raw.githubusercontent.com/kingmax1688/iptv/refs/heads/main/itvlist.m3u"
    CHECK_TIMEOUT = 3
    MAX_WORKERS = 20
    CHANNEL_ALIAS_MAP = {}
    MIN_WIDTH = 1920
    MIN_HEIGHT = 1080
    MIN_BITRATE = 2000
    ENABLE_QUALITY_CHECK = True

PLAYLIST_FILE = "playlist.m3u"


def parse_m3u(file_path_or_url, is_url=False):
    """
    解析 M3U 或 TXT 文件（本地或 URL），返回 [(频道名, URL)] 列表
    支持 M3U 格式（#EXTINF）和 TXT 格式（频道名,URL）
    优先从 tvg-name 属性提取频道名，若无则从逗号后提取
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

        # 解析 M3U 格式
        if line.startswith('#EXTINF'):
            # 1. 优先从 tvg-name 属性中提取频道名
            name = None
            tvg_match = re.search(r'tvg-name="([^"]+)"', line)
            if tvg_match:
                name = tvg_match.group(1).strip()
            # 2. 如果没有 tvg-name，则取逗号后的内容
            if not name and ',' in line:
                name = line.split(',')[-1].strip()
            # 3. 如果都没有，设为"未知频道"
            if not name:
                name = "未知频道"

            i += 1
            if i < len(lines):
                url = lines[i].strip()
                if url and not url.startswith('#'):
                    channels.append((name, url))
            i += 1
            continue

        # 解析 TXT 格式（频道名,URL）
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
    # 组播源（包含 /rtp/ 或 /udp/）直接视为达标，跳过 ffprobe 检测
    if '/rtp/' in url or '/udp/' in url:
        return True

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


def build_backup_index(sources=None):
    """
    按优先级构建备用源索引
    - 优先加载高优先级（priority 数值小）的源
    - 如果频道已存在于索引中（来自高优先级源），则不再覆盖
    - 支持两种调用方式：
      1) 传入 sources 列表（推荐）
      2) 如果没有传入，尝试使用单个 BACKUP_SOURCE_URL（向后兼容）
    """
    index = {}

    # 如果未传入 sources，尝试使用旧的 BACKUP_SOURCE_URL 模式
    if sources is None:
        try:
            url = BACKUP_SOURCE_URL
            print(f"📦 加载备用源（兼容模式）: {url}")
            channels = parse_m3u(url, is_url=True)
            for name, url in channels:
                if name not in index:
                    index[name] = []
                if url not in index[name]:
                    index[name].append(url)
            return index
        except Exception as e:
            print(f"❌ 加载备用源失败: {e}")
            return index

    # 新模式：多源按优先级加载
    # 按 priority 排序（数字小优先）
    sorted_sources = sorted(sources, key=lambda x: x.get("priority", 999))

    for source in sorted_sources:
        name = source.get("name", "未知源")
        url = source.get("url")
        priority = source.get("priority", 999)
        print(f"📦 加载备用源: {name} (优先级 {priority})")

        try:
            channels = parse_m3u(url, is_url=True)
            added = 0
            for ch_name, ch_url in channels:
                # 如果该频道尚未被更高优先级的源覆盖，则添加
                if ch_name not in index:
                    index[ch_name] = []
                # 去重
                if ch_url not in index[ch_name]:
                    index[ch_name].append(ch_url)
                    added += 1
            print(f"   ✅ 添加 {added} 条记录")
        except Exception as e:
            print(f"   ⚠️ 加载失败: {e}")

    return index


def replace_failed_channels(playlist_file, backup_index):
    """检测失效频道，从备用源替换，并过滤质量（自动去重）"""
    # 1. 读取原文件，按频道名去重，只保留第一个出现的条目
    channels_dict = {}  # {频道名: (extinf_line, url)}
    with open(playlist_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF'):
            if ',' in line:
                name = line.split(',')[-1].strip()
            else:
                name = "未知频道"
            i += 1
            if i < len(lines):
                url_line = lines[i].strip()
                if url_line and not url_line.startswith('#'):
                    if name not in channels_dict:
                        channels_dict[name] = (line, url_line)
        i += 1

    unique_channels = [(name, data[0], data[1]) for name, data in channels_dict.items()]
    print(f"📺 去重后共有 {len(unique_channels)} 个频道（原始行数可能更多）")

    # 2. 并发检测所有频道的URL
    results = {}  # {(name, url): is_valid}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(check_url, url): (name, url) for name, extinf, url in unique_channels}
        for future in as_completed(futures):
            name, url = futures[future]
            is_valid = future.result()
            results[(name, url)] = is_valid
            status = "✅" if is_valid else "❌"
            print(f"{status} {name}: {url}")

    # 3. 生成新的播放列表（仅保留去重后的频道，且每个频道只保留一条URL）
    new_lines = []
    replaced_count = 0
    failed_count = 0

    for name, extinf, current_url in unique_channels:
        # 剥离已有注释，只取纯净 URL
        clean_url = current_url.split('#')[0].strip()
        # 检查当前URL是否失效
        if results.get((name, clean_url), False) is False:  # 失效
            # 尝试从备用源中查找可用URL
            if name in backup_index and backup_index[name]:
                found = False
                for candidate_url in backup_index[name]:
                    print(f"   🔍 检查候选源: {candidate_url[:80]}...")
                    if is_quality_acceptable(candidate_url):
                        # 替换成功
                        new_lines.append(extinf + '\n')
                        new_lines.append(candidate_url + '\n')
                        replaced_count += 1
                        print(f"🔄 替换 {name}: {clean_url} → {candidate_url}")
                        found = True
                        break
                    else:
                        print(f"   ⏭️ 跳过质量不达标的候选源")
                if not found:
                    # 备用源中无合格源，保留原URL并标记
                    new_lines.append(extinf + '\n')
                    new_lines.append(clean_url + '  # 已失效，备用源质量不达标\n')
                    failed_count += 1
                    print(f"⚠️ {name}: 备用源中所有候选质量均不达标")
            else:
                # 备用源中没有该频道
                new_lines.append(extinf + '\n')
                new_lines.append(clean_url + '  # 已失效，备用源无此频道\n')
                failed_count += 1
                print(f"⚠️ {name}: 备用源中无此频道")
        else:
            # 当前URL有效，保留
            new_lines.append(extinf + '\n')
            new_lines.append(clean_url + '\n')

    # 4. 写回文件
    with open(playlist_file, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)

    print(f"✅ 完成: 替换 {replaced_count} 个，{failed_count} 个无替代源")


def main():
    print("📡 开始检测并替换失效源...")

    # 构建备用源索引
    # 优先使用多源模式（BACKUP_SOURCES），否则回退到兼容模式
    if BACKUP_SOURCES:
        backup_index = build_backup_index(BACKUP_SOURCES)
    else:
        backup_index = build_backup_index()  # 使用兼容模式

    total_urls = sum(len(v) for v in backup_index.values())
    print(f"📦 备用源共有 {len(backup_index)} 个频道，{total_urls} 条 URL")
    replace_failed_channels(PLAYLIST_FILE, backup_index)


if __name__ == "__main__":
    main()
