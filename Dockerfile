ARG UBUNTU_IMAGE=ubuntu:22.04
FROM ${UBUNTU_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    LIBMPV_BUILD_CONTAINER=1 \
    PYTHONUNBUFFERED=1

RUN apt update && apt install -y --no-install-recommends \
    ca-certificates curl git python3 python3-pip python3-venv \
    build-essential clang-14 lld-14 llvm-14-dev libstdc++-11-dev \
    cmake ninja-build pkg-config autoconf automake libtool libtool-bin \
    gettext autopoint gperf nasm yasm patch unzip xz-utils bzip2 \
    libegl1-mesa-dev libgl1-mesa-dev libx11-dev libxext-dev libxrandr-dev \
    libxpresent-dev libxss-dev libva-dev libvdpau-dev libdrm-dev \
    libpulse-dev libasound2-dev fontconfig-config fonts-dejavu-core && \
    printf 'meson==1.12.1 --hash=sha256:930bc7542cbd9f57009e182fd014eba48cf1a0180a7b9006d2d32d8f168d8b02\n' > /tmp/meson-requirements.txt && \
    python3 -m pip install --no-cache-dir --require-hashes -r /tmp/meson-requirements.txt && \

WORKDIR /workspace
ENTRYPOINT ["python3", "-m", "libmpv_build", "build"]
