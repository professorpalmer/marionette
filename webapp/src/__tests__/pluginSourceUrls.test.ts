import { describe, expect, it } from "vitest";
import {
  PluginSourceError,
  pluginInstallPayload,
  resolvePluginSource,
} from "../lib/pluginSourceUrls";

describe("pluginSourceUrls", () => {
  it("resolves absolute paths", () => {
    expect(resolvePluginSource("/abs/plugin")).toEqual({
      kind: "path",
      raw: "/abs/plugin",
      path: "/abs/plugin",
    });
    expect(resolvePluginSource("C:\\Users\\p\\plugin").kind).toBe("path");
  });

  it("resolves github https, shorthand, and github: sources", () => {
    expect(resolvePluginSource("https://github.com/acme/widget")).toEqual({
      kind: "github",
      raw: "https://github.com/acme/widget",
      owner: "acme",
      repo: "widget",
      ref: "",
      cloneUrl: "https://github.com/acme/widget.git",
    });
    expect(resolvePluginSource("https://github.com/acme/widget.git#v1")).toMatchObject({
      kind: "github",
      owner: "acme",
      repo: "widget",
      ref: "v1",
      cloneUrl: "https://github.com/acme/widget.git",
    });
    expect(resolvePluginSource("acme/widget@main")).toMatchObject({
      kind: "github",
      owner: "acme",
      repo: "widget",
      ref: "main",
      cloneUrl: "https://github.com/acme/widget.git",
    });
    expect(resolvePluginSource("github:acme/widget")).toMatchObject({
      kind: "github",
      cloneUrl: "https://github.com/acme/widget.git",
    });
  });

  it("resolves git and https remotes", () => {
    expect(resolvePluginSource("git@github.com:acme/widget.git")).toMatchObject({
      kind: "git",
      cloneUrl: "git@github.com:acme/widget.git",
      owner: "acme",
      repo: "widget",
    });
    expect(resolvePluginSource("https://gitlab.example/acme/widget.git")).toMatchObject({
      kind: "git",
      cloneUrl: "https://gitlab.example/acme/widget.git",
    });
    expect(resolvePluginSource("https://example.test/plugins/widget")).toMatchObject({
      kind: "https",
      cloneUrl: "https://example.test/plugins/widget",
    });
  });

  it("rejects empty, relative, and unsupported schemes", () => {
    expect(() => resolvePluginSource("")).toThrow(PluginSourceError);
    expect(() => resolvePluginSource("src-plugin")).toThrow(/absolute path|git URL|https URL|GitHub/);
    expect(() => resolvePluginSource("file:///tmp/plugin")).toThrow(/unsupported/);
    expect(() => resolvePluginSource("javascript:alert(1)")).toThrow(/unsupported/);
  });

  it("builds install payloads for path vs remote sources", () => {
    expect(pluginInstallPayload("/abs/plugin")).toEqual({
      source: { kind: "path", raw: "/abs/plugin", path: "/abs/plugin" },
      path: "/abs/plugin",
    });
    expect(pluginInstallPayload("https://github.com/acme/widget").path).toBeUndefined();
    expect(pluginInstallPayload("https://github.com/acme/widget").source.kind).toBe("github");
  });
});
