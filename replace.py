import re
import requests
import os
import urllib3
from concurrent.futures import ThreadPoolExecutor

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ========== 尝试导入 config.py 中的配置 ==========
try:
    from config import (
        BACKUP_SOURCES,
        MAX_WORKERS,
        CHANNEL_ALIAS_MAP,
        IGNORE_CHANNELS,
    )
except ImportError:
    BACKUP_SOURCES = None
    MAX_WORKERS = 20
    CHANNEL_ALIAS_MAP = {}
    IGNORE_CHANNELS = []

PLAYLIST_FILE = "playlist.m3u"

# ==================== ★ 新增：备用池地址配置 ====================
HOTEL_BACKUP_URL = "https://gh-proxy.com/https://raw.githubusercontent.com/kingmax1688/TV/refs/heads/main/Hotel/iptv.m3u"
MULTICAST_BACKUP_URL = "https://gh-proxy.com/https://raw.githubusercontent.com/kingmax1688/TV/refs/heads/main/my_tv/zubo_all.m3u"
# ============================================================

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}


# ==================== ★ 新增：URL 类型判断 ====================
def is_multicast_url(url):
    """判断是否组播源"""
    if not url:
        return False
    u = url.lower()
    return ('/rtp/' in u or '/udp/' in u
            or u.startswith('rtp://') or u.startswith('udp://'))


# ==================== ★ 新增：频道名归一化 ====================
def normalize_channel_name(name):
    """
    归一化频道名，用于跨源匹配。
    - 移除括号、空格
    - CCTV-1 → CCTV1
    - CCTV5+ → CCTV5PLUS
    - 移除画质标识（高清/HD/FHD/4K等）
    """
    if not name:
        return ""
    s = name.strip()
    s = re.sub(r'[【】\[\]()（）]', '', s)
    s = re.sub(r'CCTV5\+', 'CCTV5PLUS', s, flags=re.IGNORECASE)
    s = re.sub(r'CCTV[-\s]?(\d+)', r'CCTV\1', s, flags=re.IGNORECASE)
    s = re.sub(r'(高清|标清|超清|HD|FHD|UHD|4K)', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\s+', '', s)
    return s.lower()


# ==================== 解析 m3u/txt 文件 ====================
def parse_m3u(file_path_or_url, is_url=False):
    """解析 m3u 或 txt，返回 [(频道名, url)] 列表"""
    channels = []
    content = ""

    if is_url:
        resp = requests.get(file_path_or_url, timeout=15, headers=HEADERS, verify=False)
        if resp.status_code != 200:
            raise Exception(f"无法获取: HTTP {resp.status_code}")
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
                    # 去掉 URL 后面的注释
                    clean_url = url.split('#')[0].strip()
                    if clean_url:
                        channels.append((name, clean_url))
            i += 1
            continue

        # txt 格式
        if ',' in line and not line.startswith('#'):
            parts = line.split(',', 1)
            if len(parts) == 2:
                name = parts[0].strip()
                url = parts[1].strip()
                if url.startswith('http') or url.startswith('rtp://') or url.startswith('udp://'):
                    clean_url = url.split('#')[0].strip()
                    if clean_url:
                        channels.append((name, clean_url))
        i += 1

    return channels


# ==================== ★ 新增：构建归一化索引 ====================
def build_normalized_index(channels):
    """
    从 [(name, url)] 构建 {归一化频道名: [url1, url2, ...]}。
    保留原始顺序（因为备用池已经按综合分排好序）。
    """
    index = {}
    for name, url in channels:
        norm = normalize_channel_name(name)
        if not norm:
            continue
        if norm not in index:
            index[norm] = []
        if url not in index[norm]:
            index[norm].append(url)
    return index


# ==================== ★ 新增：加载备用池 ====================
def load_backup_pool():
    """
    加载两个备用池：
    - hotel_index:     酒店源，{norm_name: [urls]}（已按综合分排序）
    - multicast_index: 组播源，{norm_name: [urls]}（已按综合分排序）
    """
    hotel_index = {}
    multicast_index = {}

    print("📦 加载酒店源备用池...")
    try:
        channels = parse_m3u(HOTEL_BACKUP_URL, is_url=True)
        hotel_index = build_normalized_index(channels)
        total = sum(len(v) for v in hotel_index.values())
        print(f"   ✓ 酒店源 {len(hotel_index)} 个频道，{total} 条 URL")
    except Exception as e:
        print(f"   ❌ 酒店源加载失败: {e}")

    print("📦 加载组播源备用池...")
    try:
        channels = parse_m3u(MULTICAST_BACKUP_URL, is_url=True)
        multicast_index = build_normalized_index(channels)
        total = sum(len(v) for v in multicast_index.values())
        print(f"   ✓ 组播源 {len(multicast_index)} 个频道，{total} 条 URL")
    except Exception as e:
        print(f"   ❌ 组播源加载失败: {e}")

    return hotel_index, multicast_index


# ==================== ★ 重写：替换逻辑 ====================
def replace_channels(playlist_file, hotel_index, multicast_index):
    """
    对基表 playlist.m3u 进行替换：
    - 酒店源优先：备用池中有该频道的酒店源时，用酒店源的前 N 条替换
    - 组播源兜底：酒店源没有该频道时，用组播源的前 N 条替换
    - 都没有：标记原线路失效（不重复标记）
    """
    with open(playlist_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    # 1. 解析基表所有条目
    entries = []  # (channel_name, idx, extinf_line, clean_url)
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
                # URL 行必须以 http/rtp/udp 开头
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

    # 2. 按频道名分组
    groups = {}
    for name, idx, extinf, url in entries:
        groups.setdefault(name, []).append((idx, extinf, url))

    print(f"\n📊 共 {len(groups)} 个频道分组，开始处理...")

    replaced_count = 0    # 酒店源替换
    fallback_count = 0    # 组播源兜底
    failed_count = 0      # 标记失效
    kept_count = 0        # 保留原线路

    # 3. 逐频道处理
    for channel_name, item_list in groups.items():
        if channel_name in IGNORE_CHANNELS:
            print(f"⏭️ 跳过 {channel_name}（忽略列表）")
            continue

        norm_name = normalize_channel_name(channel_name)
        slot_count = len(item_list)

        # ★ 查找备用池
        hotel_urls = hotel_index.get(norm_name, [])
        multicast_urls = multicast_index.get(norm_name, [])

        # ★ 候选序列：酒店源优先，组播源兜底
        if hotel_urls:
            candidates = hotel_urls[:slot_count]
            source_type = "酒店源"
        elif multicast_urls:
            candidates = multicast_urls[:slot_count]
            source_type = "组播兜底"
        else:
            candidates = []
            source_type = "无备用"

        # ★ 备用池里都没有：标记失效（不重复标记）
        if not candidates:
            marked_any = False
            for idx, extinf, old_url in item_list:
                if ' # 已失效' not in lines[idx]:
                    lines[idx] = old_url + ' # 已失效\n'
                    failed_count += 1
                    marked_any = True
            if marked_any:
                print(f"❌ {channel_name}: 备用池无匹配，标记失效")
            continue

        # ★ 有候选：逐个槽位替换
        print(f"📺 {channel_name} ({source_type}): 备用池 {len(candidates)} 条 / 基表 {slot_count} 槽位")

        for i, (idx, extinf, old_url) in enumerate(item_list):
            if i < len(candidates):
                new_url = candidates[i]
                # 清理可能存在的失效标记
                if ' # 已失效' in lines[idx]:
                    lines[idx] = lines[idx].replace(' # 已失效', '')
                    lines[idx] = lines[idx].rstrip('\n') + '\n'

                if new_url == old_url:
                    kept_count += 1
                    continue

                lines[idx] = new_url + '\n'
                if source_type == "组播兜底":
                    fallback_count += 1
                else:
                    replaced_count += 1
                print(f"   🔄 槽位{i+1}: {old_url} → {new_url}")
            else:
                # ★ 备用池候选不足：保留原 URL（也可以考虑标记失效，这里选保留）
                kept_count += 1

    # 4. 写回文件
    with open(playlist_file, 'w', encoding='utf-8') as f:
        f.writelines(lines)

    print(f"\n{'='*60}")
    print(f"✅ 完成：")
    print(f"   酒店源替换: {replaced_count} 条")
    print(f"   组播源兜底: {fallback_count} 条")
    print(f"   保留原线路: {kept_count} 条")
    print(f"   标记失效:   {failed_count} 条")
    print(f"{'='*60}")


def main():
    print("📡 开始加载备用池...")
    print(f"   酒店源: {HOTEL_BACKUP_URL}")
    print(f"   组播源: {MULTICAST_BACKUP_URL}")
    print()

    hotel_index, multicast_index = load_backup_pool()

    if not hotel_index and not multicast_index:
        print("❌ 备用池加载失败，退出")
        return

    print(f"\n🔄 开始处理基表: {PLAYLIST_FILE}")
    replace_channels(PLAYLIST_FILE, hotel_index, multicast_index)


if __name__ == "__main__":
    main()
