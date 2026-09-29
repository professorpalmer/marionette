import { createElement } from "react";
import { cleanup, render } from "@testing-library/react";
import { afterEach } from "vitest";
import { TranscriptList, transcriptViewportKeys } from "../components/TranscriptList";
import type { TranscriptViewportHandle } from "../components/conversation/sessionViewport";
import { expect, it } from "vitest";
import { captureSessionViewport, sessionViewportOffset } from "../components/conversation/sessionViewport";
it("restores an interior row after variable heights above it change", () => {
 const saved = captureSessionViewport(false, 13779, () => [{key:"row",start:13700,end:14100}]);
 expect(saved).toEqual({kind:"anchor",key:"row",offset:79,scrollTop:13779});
 expect(sessionViewportOffset(saved, 15000, 18000, 600, false)).toBe(15079);
});
it("tail mode follows new content", () => {
 const saved = captureSessionViewport(true, 100, () => { throw new Error("pinned capture must not measure rows"); });
 expect(sessionViewportOffset(saved, null, 2000, 600, false)).toBe(1400);
});
it("latest user gesture cancels both anchor and tail restoration", () => {
 for (const pinned of [true,false]) {
  const saved = captureSessionViewport(pinned, 100, () => [{key:"row",start:80,end:200}]);
  expect(sessionViewportOffset(saved, 500, 2000, 600, true)).toBeNull();
 }
});
it("falls back to the saved pixel position if the anchor was deleted", () => {
 const saved = captureSessionViewport(false, 100, () => [{key:"row",start:80,end:200}]);
 expect(sessionViewportOffset(saved, null, 2000, 600, false)).toBe(100);
});

afterEach(cleanup);
it("message anchors survive rehydration and distinguish identical repeated messages", () => {
 const keys = () => transcriptViewportKeys([
  {kind:"msg",msg:{role:"user",text:"same"}},
  {kind:"msg",msg:{role:"user",text:"same"}},
 ]);
 expect(keys()).toEqual(keys());
 expect(keys()[0]).not.toEqual(keys()[1]);
});
it("mounted transcript exposes anchor capture and restores against changed row geometry", () => {
 const container = document.createElement("div");
 document.body.appendChild(container);
 const viewportRef: {current: TranscriptViewportHandle | null} = {current:null};
 const scrollContainerRef = {current:container};
 render(createElement(TranscriptList, {
  items:[{kind:"msg",msg:{role:"user",text:"interior"}}],
  status:"idle",compactingStatus:null,editingIndex:null,auto:false,plan:false,
  scrollContainerRef,viewportRef,onEditMessage:()=>{},onExecuteSend:()=>{},
  onImageClick:()=>{},onSetCard:()=>{},onExecutePlan:()=>{},onCommandApproval:()=>{},
 }), {container});
 const row = container.querySelector<HTMLElement>("[data-viewport-key]");
 if (!row || !viewportRef.current) throw new Error("Viewport handle or row missing");
 Object.defineProperty(container,"scrollHeight",{value:2000,configurable:true});
 Object.defineProperty(container,"clientHeight",{value:600,configurable:true});
 container.scrollTop = 100;
 let layoutReads = 0;
 row.getBoundingClientRect = () => { layoutReads += 1; return new DOMRect(0,-20,500,300); };
 // Pinned to the tail (every streamed token while following): no layout reads.
 expect(viewportRef.current.capture(true)).toEqual({kind:"tail"});
 expect(layoutReads).toBe(0);
 const saved = viewportRef.current.capture(false);
 expect(layoutReads).toBe(1);
 expect(saved.kind).toBe("anchor");
 row.getBoundingClientRect = () => new DOMRect(0,380,500,300);
 viewportRef.current.restore(saved);
 expect(container.scrollTop).toBe(500);
 viewportRef.current.restore({kind:"tail"});
 expect(container.scrollTop).toBe(1400);
});
