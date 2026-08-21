import vinext from "vinext";
import { defineConfig } from "vite";

// macOS Seatbelt blocks FSEvents, so Codex previews need polling for HMR.
const isCodexSeatbeltSandbox = process.env.CODEX_SANDBOX === "seatbelt";

export default defineConfig(() => {
  const apiProxyTarget =
    process.env.SCRIBE_API_PROXY_TARGET ||
    process.env.NEXT_PUBLIC_SCRIBE_API_URL ||
    "http://127.0.0.1:8000";

  return {
    server: isCodexSeatbeltSandbox
      ? {
          watch: { useFsEvents: false, usePolling: true },
          proxy: {
            "/api": {
              target: apiProxyTarget,
              changeOrigin: true,
            },
          },
        }
      : {
          proxy: {
            "/api": {
              target: apiProxyTarget,
              changeOrigin: true,
            },
          },
        },
    plugins: [vinext()],
  };
});
