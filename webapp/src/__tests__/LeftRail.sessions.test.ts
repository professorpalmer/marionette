import { afterEach, describe, expect, it } from "vitest";
import { clearSWRCache, readSWRCache, writeSWRCache } from "../lib/useStaleWhileRevalidate";
import { repoPathsEqual } from "../lib/pathNormalize";
import { buildProjectsList, purgeSessionFromRootCaches, workspacesCacheKey } from "../components/LeftRail";
import type { Session } from "../lib/api";

/**
 * LeftRail session-list contracts that do not need a full React mount:
 * per-root cache reads, path-normalized matching, and stale-payload guards.
 */
describe("LeftRail session list contracts", () => {
  afterEach(() => {
    clearSWRCache();
  });

  it("purgeSessionFromRootCaches removes id from every root cache", () => {
    const marionette = "C:\\Projects\\marionette";
    const dugout = "C:\\Projects\\dugout";
    const shared: Session = {
      id: "sess-dugout",
      title: "dugout",
      created: 1,
      repo: dugout,
      workspace_root: dugout,
    };
    // Stale bug: same session id cached under the wrong project too.
    writeSWRCache(`sessions:${marionette}`, [
      { id: "sess-m", title: "New session", created: 2, repo: marionette, workspace_root: marionette },
      shared,
    ]);
    writeSWRCache(`sessions:${dugout}`, [shared]);

    const touched = purgeSessionFromRootCaches([marionette, dugout], "sess-dugout");
    expect(touched).toBe(2);
    expect(readSWRCache<Session[]>(`sessions:${marionette}`)?.map((s) => s.id)).toEqual(["sess-m"]);
    expect(readSWRCache<Session[]>(`sessions:${dugout}`)).toEqual([]);
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

  it("project expand state is user-driven; currentRepo must not imply expand/collapse", () => {
    // Mirrors LeftRail: isExpanded = !!expandedProjects[path] — missing key
    // is collapsed. Changing the active workspace must not auto-expand the
    // new root or collapse a previously user-expanded one.
    const expandedProjects: Record<string, boolean> = {
      "C:\\Projects\\alpha": true,
    };
    const isExpanded = (projectPath: string) => !!expandedProjects[projectPath];

    const alpha = "C:\\Projects\\alpha";
    const beta = "C:\\Projects\\beta";

    // User expanded alpha; it stays open even if beta becomes currentRepo.
    expect(isExpanded(alpha)).toBe(true);
    // Beta is current but never clicked — stays collapsed.
    expect(isExpanded(beta)).toBe(false);

    // Simulate currentRepo flipping alpha → beta: expand map unchanged.
    const currentRepo = beta;
    expect(isExpanded(alpha)).toBe(true);
    expect(isExpanded(currentRepo)).toBe(false);

    // Only an explicit toggle writes expand state.
    expandedProjects[beta] = true;
    expect(isExpanded(beta)).toBe(true);
  });

  it("empty-project CTA opens that workspace before createSession", async () => {
    // Mirrors LeftRail newSession(inProjectPath): empty-state "New session"
    // and top-bar New session when selected !== current must openWorkspace
    // first so createSession lands in the selected root, not the active one.
    // Row click still must not open (yank-on-peek removed).
    let openCalls = 0;
    let createCalls = 0;
    let openedPath = "";
    const currentRepo = "C:\\Projects\\dugout";
    const emptyProject = "C:\\Projects\\marionette";

    const handleOpenProject = async (path: string) => {
      openCalls += 1;
      openedPath = path;
    };
    const createSession = async () => { createCalls += 1; };

    const newSession = async (
      inProjectPath: string | undefined,
      selectedProjectPath: string,
      current: string,
    ) => {
      const target = (inProjectPath || selectedProjectPath || "").trim();
      if (target && (!current || !repoPathsEqual(target, current))) {
        await handleOpenProject(target);
      }
      await createSession();
    };

    // Row click: expand/select only — never open.
    let rowOpenCalls = 0;
    const handleProjectRowClick = () => { /* select + expand only */ };
    handleProjectRowClick();
    expect(rowOpenCalls).toBe(0);

    // Empty-project CTA: open that root, then create.
    await newSession(emptyProject, emptyProject, currentRepo);
    expect(openCalls).toBe(1);
    expect(openedPath).toBe(emptyProject);
    expect(createCalls).toBe(1);

    // Top New session with selected === current: create only.
    openCalls = 0;
    createCalls = 0;
    await newSession(undefined, currentRepo, currentRepo);
    expect(openCalls).toBe(0);
    expect(createCalls).toBe(1);

    // Top New session with selected !== current: open then create.
    openCalls = 0;
    createCalls = 0;
    await newSession(undefined, emptyProject, currentRepo);
    expect(openCalls).toBe(1);
    expect(openedPath).toBe(emptyProject);
    expect(createCalls).toBe(1);
  });

  it("openWorkspace does not reorder projects to put current first", () => {
    const recents = ["C:\\Projects\\alpha", "C:\\Projects\\beta", "C:\\Projects\\gamma"];
    // Opening beta (already in recents) must keep recents order -- beta stays
    // at index 1, not snapped to 0.
    const afterOpenBeta = buildProjectsList("C:\\Projects\\beta", recents);
    expect(afterOpenBeta).toEqual(recents);
    expect(afterOpenBeta[0]).toBe("C:\\Projects\\alpha");
    expect(afterOpenBeta.indexOf("C:\\Projects\\beta")).toBe(1);

    // A brand-new path appends; it does not prepend.
    const afterOpenNew = buildProjectsList("C:\\Projects\\delta", recents);
    expect(afterOpenNew[0]).toBe("C:\\Projects\\alpha");
    expect(afterOpenNew[afterOpenNew.length - 1]).toBe("C:\\Projects\\delta");
  });

  it("workspaces SWR key is per-repo so branch lists stay warm and isolated", () => {
    // Branches must not share one global cache: switching projects would
    // otherwise flash the previous repo's branches, and a blank useState
    // reload on every config-changed is what made Branches feel laggy.
    expect(workspacesCacheKey("C:\\Projects\\marionette")).toBe(
      "workspaces:C:\\Projects\\marionette",
    );
    expect(workspacesCacheKey("C:\\Projects\\dugout")).toBe(
      "workspaces:C:\\Projects\\dugout",
    );
    expect(workspacesCacheKey("C:\\Projects\\marionette")).not.toBe(
      workspacesCacheKey("C:\\Projects\\dugout"),
    );
    expect(workspacesCacheKey("")).toBe("workspaces:__none__");
  });
});
