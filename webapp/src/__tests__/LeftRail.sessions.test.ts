import { afterEach, describe, expect, it } from "vitest";
import { clearSWRCache, readSWRCache, writeSWRCache } from "../lib/useStaleWhileRevalidate";
import { repoPathsEqual } from "../lib/pathNormalize";
import { buildProjectsList, canSettleSessionsForProject, collectUnreadFinishedSessionIds, filterForgottenRecent, formatLeaseExhaustedMessage, isLeaseExhaustedError, isRailWideSwitching, partitionProjectSessions, patchSessionArchivedInCaches, patchSessionSettledInCaches, projectSessionsEmptyState, purgeSessionFromRootCaches, readSessionSettledFromCaches, SESSION_LEASE_EXHAUSTED_MESSAGE, shouldOfferBackgroundStop, workspacesCacheKey } from "../components/LeftRail";
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

  it("one-shot boot expand opens the active project so session titles are visible", () => {
    // Mirrors LeftRail bootExpandedRef effect: first truthy currentRepo
    // expands once. Sessions under a project render only when isExpanded.
    let bootExpanded = false;
    let expandedProjects: Record<string, boolean> = {};
    const isExpanded = (projectPath: string) => !!expandedProjects[projectPath];
    const visibleSessionTitles = (projectPath: string, titles: string[]) =>
      isExpanded(projectPath) ? titles : [];

    const onCurrentRepo = (currentRepo: string) => {
      if (bootExpanded || !currentRepo) return;
      bootExpanded = true;
      expandedProjects = { ...expandedProjects, [currentRepo]: true };
    };

    const active = "C:\\Projects\\marionette";
    const sessionTitles = ["Last chat", "Older chat"];

    // Before boot: collapsed — session titles hidden under the project row.
    expect(isExpanded(active)).toBe(false);
    expect(visibleSessionTitles(active, sessionTitles)).toEqual([]);

    // Boot: currentRepo first becomes set → one-shot expand.
    onCurrentRepo(active);
    expect(isExpanded(active)).toBe(true);
    expect(visibleSessionTitles(active, sessionTitles)).toEqual(sessionTitles);

    // Ref already set: re-running for the same repo after a user collapse
    // must not force-expand again.
    expandedProjects = {};
    onCurrentRepo(active);
    expect(isExpanded(active)).toBe(false);
  });

  it("project expand state is user-driven after boot; subsequent currentRepo flips must not auto-expand/collapse", () => {
    // Mirrors LeftRail: boot expands the first truthy currentRepo once.
    // After that, changing the active workspace must not auto-expand the
    // new root or collapse a previously expanded one.
    let bootExpanded = false;
    const expandedProjects: Record<string, boolean> = {};
    const isExpanded = (projectPath: string) => !!expandedProjects[projectPath];

    const onCurrentRepo = (currentRepo: string) => {
      if (bootExpanded || !currentRepo) return;
      bootExpanded = true;
      expandedProjects[currentRepo] = true;
    };

    const alpha = "C:\\Projects\\alpha";
    const beta = "C:\\Projects\\beta";

    // Boot with alpha as currentRepo.
    onCurrentRepo(alpha);
    expect(isExpanded(alpha)).toBe(true);
    expect(isExpanded(beta)).toBe(false);

    // Simulate currentRepo flipping alpha → beta: expand map unchanged.
    onCurrentRepo(beta);
    expect(isExpanded(alpha)).toBe(true);
    expect(isExpanded(beta)).toBe(false);

    // Only an explicit toggle writes expand state for beta.
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

  it("buildProjectsList dedupes slash/case siblings and forget filter matches them", () => {
    const recents = ["C:\\Ashita\\Ashita", "c:/Ashita/Ashita", "C:\\Projects\\other"];
    const list = buildProjectsList("", recents);
    expect(list).toEqual(["C:\\Ashita\\Ashita", "C:\\Projects\\other"]);

    // Forgetting with an alternate spelling drops the stored form.
    expect(filterForgottenRecent(recents, "C:/ashita/ashita")).toEqual([
      "C:\\Projects\\other",
    ]);

    // Empty currentRepo after forget must not resurrect the path.
    expect(buildProjectsList("", filterForgottenRecent(recents, "C:\\Ashita\\Ashita"))).toEqual([
      "C:\\Projects\\other",
    ]);
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

  it("isLeaseExhaustedError requires lease_exhausted code (not bare 409)", () => {
    expect(isLeaseExhaustedError(new Error("/api/sessions/switch -> 409"))).toBe(false);
    expect(isLeaseExhaustedError(new Error("/api/workspace/open -> 409"))).toBe(false);
    expect(isLeaseExhaustedError(new Error("/api/sessions/create -> 409"))).toBe(false);
    expect(isLeaseExhaustedError({ code: "lease_exhausted", error: "busy" })).toBe(true);
    expect(isLeaseExhaustedError(new Error("lease_exhausted: all slots busy"))).toBe(true);
    expect(
      isLeaseExhaustedError(new Error("session runner lease exhausted: all concurrent sessions are busy")),
    ).toBe(true);
    expect(isLeaseExhaustedError(new Error("/api/sessions/switch -> 500"))).toBe(false);
    expect(isLeaseExhaustedError(new Error("/api/other -> 409"))).toBe(false);
    expect(SESSION_LEASE_EXHAUSTED_MESSAGE).toMatch(/too many sessions are busy/i);
  });

  it("isLeaseExhaustedError rejects unrelated 409 conflicts", () => {
    expect(isLeaseExhaustedError({ status: 409 })).toBe(false);
    expect(isLeaseExhaustedError({ status: 409, error: "pilot busy, try again" })).toBe(false);
    expect(isLeaseExhaustedError({ status: 409, error: "Path already exists" })).toBe(false);
    expect(isLeaseExhaustedError({ status: 409, code: "busy" })).toBe(false);
    expect(isLeaseExhaustedError({ status: 409, code: "lease_exhausted" })).toBe(true);
  });

  it("formatLeaseExhaustedMessage names busy sessions and capacity", () => {
    expect(
      formatLeaseExhaustedMessage({
        code: "lease_exhausted",
        max_concurrent: 2,
        active_count: 2,
        busy_session_titles: ["Alpha", "Beta"],
      }),
    ).toMatch(/2\/2/);
    expect(
      formatLeaseExhaustedMessage({
        code: "lease_exhausted",
        max_concurrent: 2,
        active_count: 2,
        busy_session_titles: ["Alpha", "Beta"],
      }),
    ).toMatch(/"Alpha"/);
    expect(
      formatLeaseExhaustedMessage({
        code: "lease_exhausted",
        max_concurrent: 2,
        active_count: 2,
        busy_session_titles: ["Alpha", "Beta"],
      }),
    ).toMatch(/"Beta"/);
    expect(formatLeaseExhaustedMessage({ code: "lease_exhausted" })).toBe(SESSION_LEASE_EXHAUSTED_MESSAGE);
  });

  it("shouldOfferBackgroundStop only for running non-active rows", () => {
    expect(shouldOfferBackgroundStop("running", false)).toBe(true);
    expect(shouldOfferBackgroundStop("running", true)).toBe(false);
    expect(shouldOfferBackgroundStop("idle", false)).toBe(false);
    expect(shouldOfferBackgroundStop("attaching", false)).toBe(false);
    expect(shouldOfferBackgroundStop(undefined, false)).toBe(false);
  });

  it("collectUnreadFinishedSessionIds marks background running→idle", () => {
    expect(
      collectUnreadFinishedSessionIds(
        { a: "running", b: "running", c: "idle" },
        { a: "idle", b: "idle", c: "idle" },
        "a",
      ),
    ).toEqual(["b"]);
    expect(
      collectUnreadFinishedSessionIds(
        { a: "attaching" },
        { a: "idle" },
        undefined,
      ),
    ).toEqual([]);
  });

  it("browse-select does not rail-wide switch when only jobs are transitioning", () => {
    // Selecting an already-listed project changes the jobs SWR key. That must
    // not dim the PROJECTS rail or dispatch harness-project-switching.
    expect(
      isRailWideSwitching({
        opening: false,
        switchingSessionId: null,
        workspaceTransitioning: false,
        sessionsTransitioning: false,
      }),
    ).toBe(false);

    expect(
      isRailWideSwitching({
        opening: true,
        switchingSessionId: null,
        workspaceTransitioning: false,
        sessionsTransitioning: false,
      }),
    ).toBe(true);
    expect(
      isRailWideSwitching({
        opening: false,
        switchingSessionId: "sess-1",
        workspaceTransitioning: false,
        sessionsTransitioning: false,
      }),
    ).toBe(true);
    expect(
      isRailWideSwitching({
        opening: false,
        switchingSessionId: null,
        workspaceTransitioning: true,
        sessionsTransitioning: false,
      }),
    ).toBe(true);
    expect(
      isRailWideSwitching({
        opening: false,
        switchingSessionId: null,
        workspaceTransitioning: false,
        sessionsTransitioning: true,
      }),
    ).toBe(true);
  });

  it("project session empty state is stale-while-revalidate (not jobs-gated)", () => {
    // Ready + empty => New session CTA, even if jobs/other fetches are in flight.
    expect(projectSessionsEmptyState(true, true)).toBe("empty");
    expect(projectSessionsEmptyState(true, false)).toBe("empty");
    // Not ready + selected/expanded => scoped spinner on that row only.
    expect(projectSessionsEmptyState(false, true)).toBe("loading");
    // Not ready + not selected => stay blank (no "No sessions" flash).
    expect(projectSessionsEmptyState(false, false)).toBe("pending");
  });

  it("canSettleSessionsForProject gates settle affordances to the active workspace", () => {
    const active = "C:\\Projects\\marionette";
    const other = "C:\\Projects\\other";
    expect(canSettleSessionsForProject(active, active)).toBe(true);
    expect(canSettleSessionsForProject("C:/Projects/marionette", active)).toBe(true);
    expect(canSettleSessionsForProject(other, active)).toBe(false);
    expect(canSettleSessionsForProject(active, undefined)).toBe(false);
    expect(canSettleSessionsForProject(active, "")).toBe(false);
    expect(canSettleSessionsForProject("", active)).toBe(false);
  });

  it("partitionProjectSessions splits open vs settled and scopes rootless orphans", () => {
    const root = "C:\\Projects\\marionette";
    const other = "C:\\Projects\\other";
    const rows: Session[] = [
      { id: "open-1", title: "Live", created: 3, repo: root, workspace_root: root, settled: false },
      { id: "settled-1", title: "Done", created: 2, repo: root, workspace_root: root, settled: true },
      { id: "archived-only", title: "Archived", created: 2.5, repo: root, workspace_root: root, archived: true, settled: false },
      { id: "settled-and-archived", title: "Both", created: 2.2, repo: root, workspace_root: root, archived: true, settled: true },
      { id: "orphan", title: "Orphan", created: 1, settled: false },
      { id: "other-open", title: "Other", created: 4, repo: other, workspace_root: other, settled: false },
    ];
    const active = partitionProjectSessions(rows, root, true);
    // Archived rows leave the project tree (global Archived section owns them).
    expect(active.open.map((s) => s.id)).toEqual(["open-1", "orphan"]);
    expect(active.settled.map((s) => s.id)).toEqual(["settled-1"]);

    const inactive = partitionProjectSessions(rows, root, false);
    expect(inactive.open.map((s) => s.id)).toEqual(["open-1"]);
    expect(inactive.settled.map((s) => s.id)).toEqual(["settled-1"]);
  });

  it("patchSessionSettledInCaches flips settled on matching root caches", () => {
    const marionette = "C:\\Projects\\marionette";
    const dugout = "C:\\Projects\\dugout";
    writeSWRCache(`sessions:${marionette}`, [
      { id: "sess-a", title: "A", created: 1, repo: marionette, workspace_root: marionette, settled: false, archived: false },
    ]);
    writeSWRCache(`sessions:${dugout}`, [
      { id: "sess-b", title: "B", created: 2, repo: dugout, workspace_root: dugout, settled: false },
    ]);

    expect(patchSessionSettledInCaches([marionette, dugout], "sess-a", true)).toBe(1);
    expect(readSWRCache<Session[]>(`sessions:${marionette}`)?.[0]?.settled).toBe(true);
    expect(readSWRCache<Session[]>(`sessions:${marionette}`)?.[0]?.archived).toBe(false);
    expect(readSWRCache<Session[]>(`sessions:${dugout}`)?.[0]?.settled).toBe(false);
    expect(readSessionSettledFromCaches([marionette, dugout], "sess-a")).toBe(true);

    expect(patchSessionSettledInCaches([marionette], "sess-a", false)).toBe(1);
    expect(readSWRCache<Session[]>(`sessions:${marionette}`)?.[0]?.settled).toBe(false);
  });

  it("patchSessionArchivedInCaches flips archived without touching settled", () => {
    const marionette = "C:\\Projects\\marionette";
    writeSWRCache(`sessions:${marionette}`, [
      { id: "sess-a", title: "A", created: 1, repo: marionette, workspace_root: marionette, settled: true, archived: false },
    ]);

    expect(patchSessionArchivedInCaches([marionette], "sess-a", true)).toBe(1);
    const row = readSWRCache<Session[]>(`sessions:${marionette}`)?.[0];
    expect(row?.archived).toBe(true);
    expect(row?.settled).toBe(true);

    expect(patchSessionArchivedInCaches([marionette], "sess-a", false)).toBe(1);
    expect(readSWRCache<Session[]>(`sessions:${marionette}`)?.[0]?.archived).toBe(false);
    expect(readSWRCache<Session[]>(`sessions:${marionette}`)?.[0]?.settled).toBe(true);
  });
});
