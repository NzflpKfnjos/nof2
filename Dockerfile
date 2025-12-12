FROM python:3.12-slim

# 基础环境变量
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# ===== 系统依赖（TA-Lib 必须）=====
RUN apt-get update && apt-get install -y \
    build-essential \
    wget \
    ca-certificates \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# ===== 安装 TA-Lib C 库 =====
RUN wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz \
    && tar -xzf ta-lib-0.4.0-src.tar.gz \
    && cd ta-lib \
    && ./configure --prefix=/usr \
    && make \
    && make install \
    && cd / \
    && rm -rf ta-lib ta-lib-0.4.0-src.tar.gz

# ===== Python 依赖 =====
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ===== 复制项目代码 =====
COPY . .

# 前端端口
EXPOSE 8600

# 同时启动两个进程
CMD ["bash", "-c", "python3 main.py & python3 api_history.py"]
