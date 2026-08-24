import PluginsLibrary from "./PluginsLibrary";

/** Settings Plugins page host — Agent Plugin cards + install modal. */
export default function PluginsPane({ embedded = false }: { embedded?: boolean }) {
  return (
    <div data-testid="plugins-pane">
      <PluginsLibrary embedded={embedded} />
    </div>
  );
}
