export LD_LIBRARY_PATH="$(dirname "${BASH_SOURCE[0]}")/.local/sysroot/usr/lib/x86_64-linux-gnu:$(dirname "${BASH_SOURCE[0]}")/.local/sysroot/usr/lib/x86_64-linux-gnu/pulseaudio:$(dirname "${BASH_SOURCE[0]}")/.local/sysroot/usr/lib/x86_64-linux-gnu/blas:$(dirname "${BASH_SOURCE[0]}")/.local/sysroot/usr/lib/x86_64-linux-gnu/lapack"
export PATH="$(dirname "${BASH_SOURCE[0]}")/.local/sysroot/usr/bin:$PATH"
export PLAYWRIGHT_CHROMIUM_HEADLESS_SHELL="$HOME/.cache/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell"
