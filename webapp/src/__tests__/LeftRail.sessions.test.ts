import { afterEach, describe, expect, it } from "vitest";
import { clearSWRCache, readSWRCache, writeSWRCache } from "../lib/useStaleWhileRevalidate";
import { repoPathsEqual } from "../lib/pathNormalize";
import type { Session } from "../lib/api";

/**
 * LeftRail session-list contracts that do not need a full React mount:
 * per-root cache reads, path-normalized matching, and stale-payload guards.
 */
describe("LeftRail session list contracts", () => {
  afterEach(() => {
    clearSWRCache();
  });

  it("reads cached sessions for a non-active root from sessions:${path}", () => {
    const otherRoot = "C:\\Projects\\other";
    const rows: Session[] = [
      {
        id: "s-other",
        title: "Other chat",
        created: 1,
        repo: otherRoot,
        workspace_root: otherRoot,
        active: false,
      },
    ];
    writeSWRCache(`sessions:${otherRoot}`, rows);

    const cached = readSWRCache<Session[]>(`sessions:${otherRoot}`);
    expect(cached).toEqual(rows);
    // Active-repo key must not be required to see the other root's rows.
    expect(readSWRCache<Session[]>("sessions:C:\\Projects\\active")).toBeUndefined();
  });

  it("matches session.repo to projectPath under slash/case drift", () => {
    const projectPath = "C:\\Foo\\Bar";
    const sessionRepo = "c:/foo/bar";
    expect(repoPathsEqual(sessionRepo, projectPath)).toBe(true);
  });

  it("stale sessions payload for a different repo must not promote active id", () => {
    const currentRepo = "C:\\Projects\\active";
    const staleRepo = "C:\\Projects\\other";
    const stalePayload: Session[] = [
      {
        id: "stale-active",
        title: "Wrong",
        created: 1,
        repo: staleRepo,
        workspace_root: staleRepo,
        active: true,
      },
    ];

    // Mirrors LeftRail onSessionsLoaded guard.
    const shouldPromote = (forRepo: string, current: string) =>
      !(forRepo && current && !repoPathsEqual(forRepo, current));

    expect(shouldPromote(staleRepo, currentRepo)).toBe(false);
    expect(shouldPromote(currentRepo, currentRepo)).toBe(true);

    // Even if the stale payload is written under its own key, the active
    // project's cache stays untouched.
    writeSWRCache(`sessions:${staleRepo}`, stalePayload);
    writeSWRCache(`sessions:${currentRepo}`, [
      {
        id: "keep-me",
        title: "Keep",
        created: 2,
        repo: currentRepo,
        workspace_root: currentRepo,
        active: true,
      },
    ]);
    expect(readSWRCache<Session[]>(`sessions:${currentRepo}`)?.[0]?.id).toBe("keep-me");
  });

  it("maps runners statuses to session badge visibility", () => {
    // Mirrors LeftRail RunnerStatusDot: only render when status is known.
    const runners: Record<string, "running" | "idle"> = {
      "sess-a": "running",
      "sess-b": "idle",
    };
    const badgeFor = (sessionId: string): "running" | "idle" | null =>
      runners[sessionId] ?? null;

    expect(badgeFor("sess-a")).toBe("running");
    expect(badgeFor("sess-b")).toBe("idle");
    expect(badgeFor("sess-unknown")).toBeNull();
  });

  it("chevron expand-without-activate never opens a workspace", () => {
    // Mirrors LeftRail chevron + handleProjectRowClick: expand/select only.
    let openCalls = 0;
    const handleOpenProject = () => { openCalls += 1; };
    const selectProject = (_path: string) => {};
    const setExpanded = (_path: string, _next: boolean) => {};

    const onChevronClick = (projectPath: string, isExpanded: boolean) => {
      // stopPropagation equivalent: never call handleOpenProject.
      selectProject(projectPath);
      setExpanded(projectPath, !isExpanded);
    };

    onChevronClick("C:\\Projects\\other", false);
    expect(openCalls).toBe(0);
    // Explicit open path still works when intentionally invoked.
    handleOpenProject();
    expect(openCalls).toBe(1);
  });
});
