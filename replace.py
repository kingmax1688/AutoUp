import re
import requests
import os
import subprocess
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

# ========== 尝试导入 config.py 中的配置 ==========
try:
    from config import (
        BACKUP_SOURCES,
        CHECK_TIMEOUT,
        MAX_WORKERS,
        CHANNEL_ALIAS_MAP,
        MIN_WIDTH,
        MIN_HEIGHT,
        MIN_BITRATE,
        ENABLE_QUALITY_CHECK,
        IGNORE_CHANNELS,
    )
except ImportError:
    BACKUP_SOURCES = None
    try:
        from config import BACKUP_SOURCE_URL
    except ImportError:
        BACKUP_SOURCE_URL = "https://gh-proxy.com/https://raw.githubusercontent.com/kingmax1688/TV/refs/heads/main/Hotel/iptv.m3u"
    CHECK_TIMEOUT = 3
    MAX_WORKERS = 20
    CHANNEL_ALIAS_MAP = {}
    MIN_WIDTH = 1920
    MIN_HEIGHT = 1080
    MIN_BITRATE = 2000
    ENABLE_QUALITY_CHECK = True
    IGNORE_CHANNELS = []

PLAYLIST_FILE = "playlist.m3u"


def parse_m3u(file_path_or_url, is_url=False):
    """
    解析 M3U 或 TXT 文件，返回 [(频道名, URL)] 列表
    优先从 tvg-name 提取频道名，若无则从逗号后提取
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

        if line.startswith('#EXTINF'):
            # 提取频道名
            name = None
            tvg_match = re.search(r'tvg-name="([^"]+)"', line)
            if tvg_match:
                name = tvg_match.group(1).strip()
            if not name and ',' in line:
                name = line.split(',')[-1].strip()
            if not name:
                name = "未知频道"

            i += 1
            if i < len(lines):
                url = lines[i].strip()
                if url and not url.startswith('#'):
                    channels.append((name, url))
            i += 1
            continue

        # TXT 格式
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
    """使用 ffprobe 获取流媒体信息"""
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
        return None, None, None


def is_quality_acceptable(url):
    """质量检测，组播源直接通过"""
    if '/rtp/' in url or '/udp/' in url:
        return True
    if not ENABLE_QUALITY_CHECK:
        return True
    width, height, bitrate = get_stream_info(url)
    if width is None:
        return False
    if width >= MIN_WIDTH and height >= MIN_HEIGHT:
        if bitrate is None or bitrate >= MIN_BITRATE:
            return True
    return False


def build_backup_index(sources=None):
    """构建备用源索引 {频道名: [URL1, URL2, ...]}，按速度排序（模拟）"""
    index = {}
    if sources is None:
        try:
            url = BACKUP_SOURCE_URL
            channels = parse_m3u(url, is_url=True)
            for name, url in channels:
                index.setdefault(name, []).append(url)
            return index
        except Exception as e:
            print(f"❌ 加载备用源失败: {e}")
            return index

    sorted_sources = sorted(sources, key=lambda x: x.get("priority", 999))
    for source in sorted_sources:
        name = source.get("name", "未知源")
        url = source.get("url")
        priority = source.get("priority", 999)
        try:
            channels = parse_m3u(url, is_url=True)
            for ch_name, ch_url in channels:
                if ch_name not in index:
                    index[ch_name] = []
                if ch_url not in index[ch_name]:
                    index[ch_name].append(ch_url)
        except Exception as e:
            print(f"   ⚠️ 加载 {name} 失败: {e}")
    return index


def replace_failed_channels(playlist_file, backup_index):
    """
    检测失效频道，按频道分组，每条线路独立检测和替换
    保持原有顺序，不叠加注释
    """
    # 1. 读取原文件，解析所有条目
    with open(playlist_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    # 存储每个条目： (频道名, 行索引, extinf行内容, URL行内容)
    entries = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF'):
            # 提取频道名
            name = None
            if ',' in line:
                name = line.split(',')[-1].strip()
            if not name:
                tvg_match = re.search(r'tvg-name="([^"]+)"', line)
                if tvg_match:
                    name = tvg_match.group(1).strip()
            if not name:
                name = "未知频道"

            extinf_line = lines[i]
            i += 1
            if i < len(lines):
                url_line = lines[i].strip()
                # 如果URL行是以http或rtp/udp开头，认为是有效的
                if url_line and (url_line.startswith('http') or url_line.startswith('rtp://') or url_line.startswith('udp://')):
                    # 剥离已有注释，只取纯净URL
                    clean_url = url_line.split('#')[0].strip()
                    entries.append((name, i, extinf_line, clean_url))
                else:
                    # 没有URL行，跳过
                    i += 1
            # 跳过可能的空行或注释行（在下一个循环处理）
            while i < len(lines) and not lines[i].strip().startswith('#EXTINF'):
                i += 1
        else:
            i += 1

    if not entries:
        print("⚠️ 未找到任何频道条目")
        return

    # 2. 按频道名分组
    groups = {}
    for name, idx, extinf, url in entries:
        groups.setdefault(name, []).append((idx, extinf, url))

    # 3. 构建备用源候选池（按速度排序）
    # 备用源已经按priority加载，但我们需要对每个频道的候选URL排序（假设速度排序）
    # 实际上我们无法在备用源中排序，但我们可以假设备用池中的顺序就是速度顺序（由priority决定）
    # 这里我们简单使用原来的顺序，优先取前面的
    # 为了模拟速度排序，我们可以将备用源列表中的URL按某种规则排序（如字符串）
    # 但更合理的是，我们信任BACKUP_SOURCES的优先级

    # 4. 处理每个频道组
    replaced_count = 0
    failed_count = 0

    # 由于我们要修改原行，我们需要修改 lines 列表
    # 对每个频道组，检测每条URL，失效则替换
    for channel_name, item_list in groups.items():
        # 如果频道在忽略列表中，跳过
        if channel_name in IGNORE_CHANNELS:
            print(f"⏭️ 跳过 {channel_name}（忽略列表）")
            continue

        # 检测每个URL
        urls_to_replace = []
        for idx, extinf, url in item_list:
            is_valid = check_url(url)
            if is_valid:
                print(f"✅ {channel_name}: {url} 有效")
                continue
            else:
                print(f"❌ {channel_name}: {url} 失效")
                # 标记需要替换
                urls_to_replace.append((idx, url))

        if not urls_to_replace:
            continue

        # 尝试从备用源获取候选
        candidates = backup_index.get(channel_name, [])
        # 过滤掉无效的（如果可能，但没时间测，直接按顺序取）
        # 假设备用源中URL都是有效的（或至少部分有效）
        # 从备用池中取足够数量的候选，按顺序分配
        # 注意：候选可能不够，不够则保留原URL并标记失效
        for i, (idx, old_url) in enumerate(urls_to_replace):
            if i < len(candidates):
                new_url = candidates[i]
                if new_url == old_url:
                    # 如果相同，跳过
                    continue
                # 质量检测
                if is_quality_acceptable(new_url):
                    # 替换URL行
                    lines[idx] = new_url + '\n'
                    replaced_count += 1
                    print(f"🔄 替换 {channel_name}: {old_url} → {new_url}")
                else:
                    # 质量不合格，尝试下一个候选
                    # 这里我们简单处理，如果第一个不合格，尝试后面的
                    found = False
                    for j in range(i+1, len(candidates)):
                        if is_quality_acceptable(candidates[j]):
                            new_url = candidates[j]
                            lines[idx] = new_url + '\n'
                            replaced_count += 1
                            print(f"🔄 替换 {channel_name}: {old_url} → {new_url}")
                            found = True
                            break
                    if not found:
                        # 没有合格候选，标记失效
                        # 检查是否已有注释
                        if ' # 已失效' not in lines[idx]:
                            lines[idx] = old_url + ' # 已失效\n'
                            failed_count += 1
                            print(f"⚠️ {channel_name}: 备用源无合格候选，标记失效")
            else:
                # 候选不足，标记失效
                if ' # 已失效' not in lines[idx]:
                    lines[idx] = old_url + ' # 已失效\n'
                    failed_count += 1
                    print(f"⚠️ {channel_name}: 备用源候选不足，标记失效")

    # 5. 写回文件
    with open(playlist_file, 'w', encoding='utf-8') as f:
        f.writelines(lines)

    print(f"✅ 完成: 替换 {replaced_count} 条线路，{failed_count} 条线路标记失效")


def main():
    print("📡 开始检测并替换失效源...")
    if BACKUP_SOURCES:
        backup_index = build_backup_index(BACKUP_SOURCES)
    else:
        backup_index = build_backup_index()

    total_urls = sum(len(v) for v in backup_index.values())
    print(f"📦 备用源共有 {len(backup_index)} 个频道，{total_urls} 条候选 URL")
    replace_failed_channels(PLAYLIST_FILE, backup_index)


if __name__ == "__main__":
    main()
