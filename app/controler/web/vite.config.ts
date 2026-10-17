import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  base: "/console/",
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
  plugins: [
    react(),
    tailwindcss(),
    {
      name: "development-csp",
      apply: "serve",
      // HMR injects a React preamble and styles. Production retains the strict HTML CSP.
      transformIndexHtml: {
        order: "pre",
        handler: (html) =>
          html.replace(
            /<meta\s+http-equiv="Content-Security-Policy"[\s\S]*?\/>/,
            "",
          ),
      },
    },
  ],
  resolve: { alias: { "@": fileURLToPath(new URL(".", import.meta.url)) } },
  build: { outDir: "dist" },
});
