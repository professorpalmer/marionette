import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterEach } from "vitest";
import "../index.css";

if (typeof HTMLDialogElement !== "undefined") {
  const proto = HTMLDialogElement.prototype;
  if (typeof proto.showModal !== "function") {
    Object.defineProperty(proto, "showModal", {
      configurable: true,
      value: function (this: HTMLDialogElement) {
        this.setAttribute("open", "");
      },
    });
  }
  if (typeof proto.close !== "function") {
    Object.defineProperty(proto, "close", {
      configurable: true,
      value: function (this: HTMLDialogElement) {
        this.removeAttribute("open");
      },
    });
  }
}

// findBy*/waitFor default to 1000ms. CI runs this suite 7-9x slower than a
// dev machine, which put ordinary cold renders past that. Queries still
// resolve the moment the element appears; only a real miss waits this long.
configure({ asyncUtilTimeout: 4000 });

afterEach(() => {
  cleanup();
});
