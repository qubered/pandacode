# Builds pandacode-core, the headless core for remote mode (docs/remote-core.md).
#
#   docker build -f docker/pandacode-core.Dockerfile --target export --output dist-core .
#   docker build -f docker/pandacode-core.Dockerfile --platform linux/amd64 --target export --output dist-core .
#
# The binary lands in dist-core/. Copy it to the server: there it needs only the
# GTK 3 runtime (`apt install libgtk-3-0`), git, and the coding CLIs you use.
# Tauri's Linux crates need the GTK and WebKitGTK headers to compile, so they stay
# in this build stage; the binary never loads WebKit.
#
# Ubuntu 22.04 keeps glibc at 2.35, so the binary also runs on Debian 12 and newer Ubuntus.
FROM ubuntu:22.04 AS build
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl build-essential pkg-config libssl-dev \
      libgtk-3-dev libwebkit2gtk-4.1-dev \
 && rm -rf /var/lib/apt/lists/*
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain 1.98.1
ENV PATH=/root/.cargo/bin:$PATH
WORKDIR /src
COPY src-tauri src-tauri
# The bots web chat serves the frontend's Markdown parser (bots/channels/http.rs).
COPY src/vendor src/vendor
RUN --mount=type=cache,target=/root/.cargo/registry \
    --mount=type=cache,target=/src/src-tauri/target \
    cargo build --release --locked --manifest-path src-tauri/Cargo.toml \
      --no-default-features --features headless --bin pandacode-core \
 && cp src-tauri/target/release/pandacode-core /pandacode-core

FROM scratch AS export
COPY --from=build /pandacode-core /pandacode-core

# Optional: a runtime image with only the GTK 3 runtime and git. Agents need their
# CLIs inside it too, so running the binary directly on the server is usually simpler.
FROM ubuntu:22.04 AS runtime
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgtk-3-0 git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /pandacode-core /usr/local/bin/pandacode-core
EXPOSE 7878
ENTRYPOINT ["pandacode-core", "--bind", "0.0.0.0"]
