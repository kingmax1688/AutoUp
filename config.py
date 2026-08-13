# config.py（更新部分）

# ========== 多备用源配置（按优先级） ==========
BACKUP_SOURCES = [
    {
        "name": "酒店源 (B仓库)",
        "url": "https://gh-proxy.com/https://raw.githubusercontent.com/kingmax1688/TV/refs/heads/main/Hotel/iptv.m3u",
        "priority": 1
    },
    {
        "name": "组播源（B仓库）",
        "url": "https://gh-proxy.com/https://raw.githubusercontent.com/kingmax1688/TV/refs/heads/main/my_tv/zubo_all.m3u",
        "priority": 2
    },
    {
        "name": "zilong7728/Collect-IPTV",
        "url": "https://ghproxy.net/https://raw.githubusercontent.com/zilong7728/Collect-IPTV/refs/heads/main/best_sorted.m3u",
        "priority": 3
    },
    # 可以继续添加更多备用源，按需要调整 priority 数值
]

# 其他配置保持不变
CHECK_TIMEOUT = 3
MAX_WORKERS = 20
CHANNEL_ALIAS_MAP = {}
MIN_WIDTH = 1920
MIN_HEIGHT = 1080
MIN_BITRATE = 2000
ENABLE_QUALITY_CHECK = True

IGNORE_CHANNELS = [
    "经典港剧一",
    "经典港剧二",
    "经典港剧三",
    "经典港剧四",
    "经典港剧五",
    "经典港剧六",
    "爱情公寓",
    "仙剑奇侠传",
    # 添加你不想参与检测和替换的频道名（必须与 playlist.m3u 中完全一致）
]
