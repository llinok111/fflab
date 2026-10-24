"""fflab -- FFmpeg filtergraph 交互式练习台.

对外只暴露三层:
  sandbox   -- 隔离目录、路径守卫、素材重建、重置
  runner    -- 执行 ffmpeg 命令并采集机器可读度量
  tutorial  -- 步骤定义、命令校验、结果断言、进度记录
"""

__version__ = "1.0.0"
