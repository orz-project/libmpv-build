ARG UBUNTU_IMAGE=ubuntu:22.04
FROM ${UBUNTU_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    LIBMPV_BUILD_CONTAINER=1 \
    PYTHONUNBUFFERED=1

COPY dependencies.json /tmp/libmpv-dependencies.json

RUN apt update && apt install -y --no-install-recommends \
    ca-certificates curl gnupg git python3 python3-pip python3-venv \
    build-essential libstdc++-11-dev \
    cmake ninja-build pkg-config autoconf automake libtool libtool-bin \
    gettext autopoint gperf nasm yasm patch unzip xz-utils bzip2 \
    libegl1-mesa-dev libgl1-mesa-dev libx11-dev libxext-dev libxrandr-dev \
    libxpresent-dev libxss-dev libva-dev libvdpau-dev libdrm-dev \
    libwayland-dev libxkbcommon-dev libffi-dev libexpat1-dev \
    libpulse-dev libasound2-dev fontconfig-config fonts-dejavu-core

RUN curl -fsSL --retry 3 --retry-all-errors --connect-timeout 30 --max-time 120 \
        https://apt.llvm.org/llvm-snapshot.gpg.key -o /tmp/llvm-snapshot.gpg.key && \
    gpg --dearmor -o /usr/share/keyrings/llvm-archive-keyring.gpg /tmp/llvm-snapshot.gpg.key && \
    task_llvm_version="$(python3 -c 'import json, re; lock = json.load(open("/tmp/libmpv-dependencies.json")); version = lock["toolchains"]["linux"]["llvm"]; assert re.fullmatch(r"[0-9]+", version); print(version)')" && \
    echo "deb [signed-by=/usr/share/keyrings/llvm-archive-keyring.gpg] https://apt.llvm.org/jammy/ llvm-toolchain-jammy-${task_llvm_version} main" > /etc/apt/sources.list.d/llvm.list && \
    apt update && apt install -y --no-install-recommends "clang-${task_llvm_version}" "lld-${task_llvm_version}" "llvm-${task_llvm_version}" && \
    python3 -c 'import json; record = json.load(open("/tmp/libmpv-dependencies.json"))["buildTools"]["meson"]; requirement = "meson==" + record["version"] if "wheelSha256" in record else "meson @ " + record["url"]; digest = record.get("wheelSha256", record["sha256"]); open("/tmp/meson-requirements.txt", "w").write(requirement + " --hash=sha256:" + digest + "\n")' && \
    python3 -m pip install --no-cache-dir --require-hashes -r /tmp/meson-requirements.txt && \
    rm -rf /var/lib/apt/lists/* /tmp/llvm-snapshot.gpg.key /tmp/meson-requirements.txt /tmp/libmpv-dependencies.json

WORKDIR /workspace
ENTRYPOINT ["python3", "-m", "libmpv_build", "build"]
