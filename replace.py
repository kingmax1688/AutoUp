import re
import time
import requests
import os
import subprocess
import json
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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

# ==================== 新增：评分相关配置 ====================
SPEED_SAMPLE_KB   = 64      # 测速采样大小（KB），越大越准但越慢
SPEED_TIMEOUT     = 6       # 测速超时（秒）
FFPROBE_TIMEOUT   = 8       # ffprobe 超时（秒）
SPEED_WEIGHT      = 0.4     # 速度权重
QUALITY_WEIGHT    = 0.6     # 质量权重
SPEED_FULL_MARK   = 2000    # 达到该速度（KB/s）即速度满分
REPLACE_THRESHOLD = 1.05    # 新候选分数需高于原候选这个倍数才替换（避免频繁抖动）
# ==========================================================

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}


# ==================== 原有解析函数（保持不变） ====================
def parse_m3u(file_path_or_url, is_url=False):
    channels = []
    content = ""

    if is_url:
        resp = requests.get(file_path_or_url, timeout=10, headers=HEADERS, verify=False)
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
    """原有 HEAD 检测，保留作为快速探活使用"""
    if '/rtp/' in url or '/udp/' in url:
        return True
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True,
                          headers=HEADERS, verify=False)
        return r.status_code == 200
    except Exception:
        return False


def get_stream_info(url):
    """使用 ffprobe 获取流媒体信息（宽、高、码率）"""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,bit_rate",
            "-of", "json",
            url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT)
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
    except Exception:
        return None, None, None


# ==================== 新增：探测 + 测速 ====================
def probe_url(url):
    """
    一次 GET 请求同时判断可用性并测量下载速度。
    返回 (valid: bool, speed_kbps: float|None, elapsed: float|None)
    - 组播源直接通过，不测速
    """
    if '/rtp/' in url or '/udp/' in url:
        return True, None, None

    sample_bytes = SPEED_SAMPLE_KB * 1024
    try:
        start = time.time()
        r = requests.get(url, timeout=SPEED_TIMEOUT, stream=True,
                         headers=HEADERS, verify=False, allow_redirects=True)
        if r.status_code != 200:
            r.close()
            return False, None, None

        downloaded = 0
        for chunk in r.iter_content(chunk_size=8192):
            downloaded += len(chunk)
            if downloaded >= sample_bytes:
                break
            if time.time() - start > SPEED_TIMEOUT:
                break

        elapsed = time.time() - start
        r.close()

        if downloaded == 0 or elapsed <= 0:
            return False, None, None

        speed_kbps = (downloaded / 1024) / elapsed
        return True, speed_kbps, elapsed
    except Exception:
        return False, None, None


# ==================== 新增：质量评分 & 综合评分 ====================
def compute_quality_score(width, height, bitrate_kbps):
    """根据分辨率与码率返回 0-100 的质量分"""
    if not width or not height:
        # 拿不到信息时给中等分，避免误杀
        return 50.0

    pixel_count = width * height
    if pixel_count >= 3840 * 2160:
        score = 100.0
    elif pixel_count >= 1920 * 1080:
        score = 80.0
    elif pixel_count >= 1280 * 720:
        score = 60.0
    else:
        score = 30.0

    if bitrate_kbps:
        if bitrate_kbps >= 8000:
            score = min(100.0, score + 20)
        elif bitrate_kbps >= 4000:
            score = min(100.0, score + 10)
    return score


def compute_speed_score(speed_kbps):
    """速度分，0-100，SPEED_FULL_MARK KB/s 即满分"""
    if speed_kbps is None:
        return 30.0  # 无法测速给个中性分
    return min(100.0, speed_kbps / SPEED_FULL_MARK * 100.0)


def score_url(url):
    """
    对单个 URL 做综合评分，返回 dict：
    {
        url, valid, speed_kbps, width, height, bitrate_kbps, score
    }
    """
    result = {
        "url": url,
        "valid": False,
        "speed_kbps": None,
        "width": None,
        "height": None,
        "bitrate_kbps": None,
        "score": 0.0,
    }

    # 组播源：跳过检测与测速
    if '/rtp/' in url or '/udp/' in url:
        result["valid"] = True
        result["score"] = 55.0  # 中性分
        return result

    valid, speed_kbps, _ = probe_url(url)
    if not valid:
        return result

    result["valid"] = True
    result["speed_kbps"] = speed_kbps

    # 只有可用 URL 才做质量检测，减少 ffprobe 调用量
    width, height, bitrate = get_stream_info(url)
    result["width"] = width
    result["height"] = height
    result["bitrate_kbps"] = bitrate

    speed_score = compute_speed_score(speed_kbps)
    quality_score = compute_quality_score(width, height, bitrate)
    result["score"] = speed_score * SPEED_WEIGHT + quality_score * QUALITY_WEIGHT

    return result


# ==================== 备用源索引构建（保持不变） ====================
def build_backup_index(sources=None):
    index = {}
    if sources is None:
        try:
            url = BACKUP_SOURCE_URL
            channels = parse_m3u(url, is_url=True)
            for name, u in channels:
                index.setdefault(name, []).append(u)
            return index
        except Exception as e:
            print(f"❌ 加载备用源失败: {e}")
            return index

    sorted_sources = sorted(sources, key=lambda x: x.get("priority", 999))
    for source in sorted_sources:
        name = source.get("name", "未知源")
        url = source.get("url")
        try:
            channels = parse_m3u(url, is_url=True)
            for ch_name, ch_url in channels:
                index.setdefault(ch_name, [])
                if ch_url not in index[ch_name]:
                    index[ch_name].append(ch_url)
        except Exception as e:
            print(f"   ⚠️ 加载 {name} 失败: {e}")
    return index


# ==================== 重写：择优替换 ====================
def replace_failed_channels(playlist_file, backup_index):
    """
    对每个频道的每条线路：
      1. 收集所有候选（现有 playlist URL + 备用源 URL）
      2. 并发对所有候选做 探测 + 测速 + 质量评分
      3. 按综合分数排序，选出最优的 N 条（N=原频道线路数）
      4. 如果原 URL 仍是最高分之一，保留；否则用更优候选替换
    """
    # 1. 读取原文件
    with open(playlist_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    # 2. 解析所有条目
    entries = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF'):
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
                if url_line and (url_line.startswith('http')
                                 or url_line.startswith('rtp://')
                                 or url_line.startswith('udp://')):
                    clean_url = url_line.split('#')[0].strip()
                    entries.append((name, i, extinf_line, clean_url))
                else:
                    i += 1
            while i < len(lines) and not lines[i].strip().startswith('#EXTINF'):
                i += 1
        else:
            i += 1

    if not entries:
        print("⚠️ 未找到任何频道条目")
        return

    # 3. 按频道分组
    groups = {}
    for name, idx, extinf, url in entries:
        groups.setdefault(name, []).append((idx, extinf, url))

    print(f"📊 共 {len(groups)} 个频道分组，开始收集候选...")

    # 4. 收集所有候选（原 URL + 备用源），去重
    channel_candidates = {}
    all_urls = set()
    for channel_name, item_list in groups.items():
        if channel_name in IGNORE_CHANNELS:
            continue
        existing_urls = [url for _, _, url in item_list]
        backup_urls = backup_index.get(channel_name, [])
        # 保留顺序去重
        merged = list(dict.fromkeys(existing_urls + backup_urls))
        channel_candidates[channel_name] = merged
        all_urls.update(merged)

    print(f"🔍 待评分 URL 总数: {len(all_urls)}")

    # 5. 并发评分
    score_map = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_url = {executor.submit(score_url, u): u for u in all_urls}
        done = 0
        for future in as_completed(future_to_url):
            u = future_to_url[future]
            try:
                score_map[u] = future.result()
            except Exception as e:
                print(f"   ⚠️ 评分异常 {u}: {e}")
                score_map[u] = {"url": u, "valid": False, "score": 0.0}
            done += 1
            if done % 20 == 0 or done == len(all_urls):
                print(f"   进度: {done}/{len(all_urls)}")

    # 6. 每个频道择优替换
    replaced_count = 0
    failed_count = 0
    kept_count = 0

    for channel_name, item_list in groups.items():
        if channel_name in IGNORE_CHANNELS:
            print(f"⏭️ 跳过 {channel_name}（忽略列表）")
            continue

        slot_count = len(item_list)  # 保留原有线路数量
        candidates = channel_candidates.get(channel_name, [])

        # 只保留 valid 的候选，按分数降序
        valid_candidates = [
            (u, score_map[u]) for u in candidates
            if u in score_map and score_map[u].get("valid")
        ]
        valid_candidates.sort(key=lambda x: x[1]["score"], reverse=True)

        best = valid_candidates[:slot_count]

        for i, (idx, extinf, old_url) in enumerate(item_list):
            if i >= len(best):
                # 没有足够候选，标记原 URL 失效
                if ' # 已失效' not in lines[idx]:
                    lines[idx] = old_url + ' # 已失效\n'
                    failed_count += 1
                    print(f"⚠️ {channel_name}: 无可用候选，标记失效")
                continue

            new_url, new_info = best[i]
            new_score = new_info["score"]

            if new_url == old_url:
                kept_count += 1
                print(f"✅ {channel_name}: 保留 {old_url} (评分 {new_score:.1f})")
                continue

            # 判断是否值得替换：新候选分数需明显高于原候选
            old_info = score_map.get(old_url, {"score": 0.0, "valid": False})
            old_score = old_info.get("score", 0.0)

            if not old_info.get("valid") or new_score >= old_score * REPLACE_THRESHOLD:
                lines[idx] = new_url + '\n'
                replaced_count += 1
                print(f"🔄 {channel_name}: {old_url} ({old_score:.1f}) → {new_url} ({new_score:.1f})")
            else:
                kept_count += 1
                print(f"✅ {channel_name}: 原线路更优，保留 {old_url} ({old_score:.1f})")

    # 7. 写回文件
    with open(playlist_file, 'w', encoding='utf-8') as f:
        f.writelines(lines)

    print(f"\n✅ 完成: 替换 {replaced_count} 条 / 保留 {kept_count} 条 / 标记失效 {failed_count} 条")


def main():
    print("📡 开始检测、评分并择优替换直播源...")
    if BACKUP_SOURCES:
        backup_index = build_backup_index(BACKUP_SOURCES)
    else:
        backup_index = build_backup_index()

    total_urls = sum(len(v) for v in backup_index.values())
    print(f"📦 备用源共有 {len(backup_index)} 个频道，{total_urls} 条候选 URL")
    replace_failed_channels(PLAYLIST_FILE, backup_index)


if __name__ == "__main__":
    main()
