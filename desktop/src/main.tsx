import React from "react";
import ReactDOM from "react-dom/client";
import { createTheme, MantineProvider, Modal } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";
import "./styles.css";
import App from "./App";
const theme = createTheme({
  primaryColor: "cyan",
  primaryShade: 9,
  defaultRadius: "sm",
  components: {
    Modal: Modal.extend({
      defaultProps: {
        overlayProps: { backgroundOpacity: 0.5, blur: 5 },
        closeButtonProps: { "aria-label": "关闭对话框" },
      },
    }),
  },
  fontFamily:
    '-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif',
  headings: { fontFamily: "inherit" },
  colors: {
    dark: [
      "#d8dfe7",
      "#b3bdc9",
      "#8995a5",
      "#617082",
      "#3a4655",
      "#283340",
      "#1b2430",
      "#131b25",
      "#0e151e",
      "#090f16",
    ],
  },
  defaultGradient: { from: "cyan", to: "blue" },
});
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <MantineProvider theme={theme} forceColorScheme="dark">
      <Notifications position="bottom-right" />
      <App />
    </MantineProvider>
  </React.StrictMode>,
);
