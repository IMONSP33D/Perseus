# The code runner's sandbox image: every toolchain in perseus/sandbox/languages.py.
#
# Build it once into an E2B template (needs only your E2B key, no Docker):
#   uv run python -m setup.sandbox.build_template --key-file ~/.config/perseus/e2b.key
# then set [code_runner] e2b_template = "perseus-code" in perseus.toml.
#
# Languages whose toolchain is missing are simply not offered (the broker probes at start-up),
# so a smaller image works too. E2B templates must be Debian-based.
FROM e2bdev/base

USER root
ENV DEBIAN_FRONTEND=noninteractive

# C, C++, Go, Java; Python and Node.js only if the base image doesn't already have them
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential gcc g++ golang-go default-jdk-headless \
      ca-certificates curl \
 && (command -v python3 >/dev/null || apt-get install -y --no-install-recommends python3) \
 && (command -v node >/dev/null || apt-get install -y --no-install-recommends nodejs) \
 && rm -rf /var/lib/apt/lists/*

# Rust: a minimal toolchain, linked into /usr/local/bin so it works without rustup's environment
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | sh -s -- -y --profile minimal --default-toolchain stable --no-modify-path \
 && ln -sf "$(/usr/local/cargo/bin/rustup which rustc)" /usr/local/bin/rustc \
 && chmod -R a+rX /usr/local/rustup /usr/local/cargo

# fail the build here, not at boot, if a toolchain didn't install
RUN python3 --version && node --version && gcc --version | head -1 && g++ --version | head -1 \
 && rustc --version && go version && java -version
