# config.py
# 所有可配置参数集中在这里

# ========== 备用源仓库B的配置 ==========
# 仓库B的 raw 链接（公开仓库直接用 raw 链接，私有仓库需带 token）
BACKUP_SOURCE_URL = "https://raw.githubusercontent.com/你的用户名/仓库B/main/backup.txt"

# 如果仓库B是私有的，使用带 token 的链接（在 GitHub Secrets 中配置 TOKEN）
# BACKUP_SOURCE_URL = f"https://{os.environ.get('BACKUP_TOKEN')}@raw.githubusercontent.com/你的用户名/仓库B/main/backup.txt"

# ========== 检测参数 ==========
CHECK_TIMEOUT = 3          # 检测单个URL的超时时间（秒）
MAX_WORKERS = 20           # 并发检测的线程数

# ========== 频道别名映射 ==========
# 格式：备用源中的名称 : 你的基准列表中的名称
# 当备用源的频道名与你的基准列表不一致时，在这里建立映射
CHANNEL_ALIAS_MAP = {
    # 示例：
    # "湖南卫视高清": "湖南卫视",
    # "CCTV1综合": "CCTV-1 综合",
    # "东方卫视HD": "东方卫视",
}

# ========== 质量阈值 ==========
MIN_WIDTH = 1920          # 最小宽度（像素），通常 1080P 对应 1920
MIN_HEIGHT = 1080         # 最小高度（像素）
MIN_BITRATE = 2000        # 最小码率（kbps），例如 2000 kbps = 2 Mbps

# 是否启用质量检测（True/False）
ENABLE_QUALITY_CHECK = True
