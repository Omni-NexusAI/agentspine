FROM nvidia/cuda:12.6.3-devel-ubuntu22.04 AS engine
ARG AUDIO_CPP_REF=238ab6a9e321c17de8e120559f57efeedaeb1345
ARG AUDIO_CPP_CUDA_ARCHITECTURES=86
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates cmake git ninja-build build-essential python3 && rm -rf /var/lib/apt/lists/*
RUN git clone --no-checkout https://github.com/0xShug0/audio.cpp.git /opt/audio.cpp && cd /opt/audio.cpp && git fetch --depth 1 origin "$AUDIO_CPP_REF" && git checkout --detach "$AUDIO_CPP_REF"
WORKDIR /opt/audio.cpp
COPY sidecar/audiocpp/patches/qwen3-native-pcm-streaming.patch /tmp/native.patch
RUN git apply --check /tmp/native.patch && git apply /tmp/native.patch
RUN CUDAARCHS="$AUDIO_CPP_CUDA_ARCHITECTURES" bash scripts/build_linux.sh --backend cuda --deployment-build --model-set custom --models qwen3_tts --target audiocpp_server --jobs 2

FROM nvidia/cuda:12.6.3-runtime-ubuntu22.04
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AUDIOCPP_BACKEND=cuda AUDIO_CPP_SERVER_BIN=/usr/local/bin/audiocpp_server VOICE_LIBRARY_DIR=/voices AUDIO_CPP_NATIVE_INCREMENTAL_PCM=false
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip ffmpeg libgomp1 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY sidecar/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt && useradd --uid 10001 --create-home convo && mkdir -p /opt/models /voices && chown 10001:10001 /opt/models /voices
COPY --from=engine /opt/audio.cpp/build/linux-cuda-release/bin/audiocpp_server /usr/local/bin/audiocpp_server
COPY --from=engine /opt/audio.cpp/model_specs /opt/audio.cpp/model_specs
COPY --from=engine /opt/audio.cpp/assets /opt/audio.cpp/assets
COPY --from=engine /opt/audio.cpp/LICENSE /opt/audio.cpp/LICENSE
COPY sidecar/audiocpp/qwen3-tts-base-f16.json /config/qwen3-tts-base-f16.json
COPY sidecar/audiocpp /app
WORKDIR /app
VOLUME ["/opt/models", "/voices"]
USER 10001
EXPOSE 8080
CMD ["python3", "-m", "uvicorn", "gateway:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
