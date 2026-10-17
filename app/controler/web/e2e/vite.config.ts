import { fileURLToPath } from "node:url";
import { defineConfig, mergeConfig } from "vite";
import config from "../vite.config.ts";

export default mergeConfig(
  config,
  defineConfig({
    plugins: [
      {
        name: "regression-activity-reader",
        enforce: "pre",
        resolveId(source, importer) {
          if (
            source === "./fixtures" &&
            importer?.endsWith("/useConversation.ts")
          )
            return fileURLToPath(
              new URL("./activity-reader.ts", import.meta.url),
            );
        },
      },
    ],
  }),
);
