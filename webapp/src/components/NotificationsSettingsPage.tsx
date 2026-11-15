import { useState } from "react";
import {
  NOTIFY_PREF_KEY,
  SOUND_PREF_KEY,
  notifyPrefEnabled,
  soundPrefEnabled,
} from "./conversation/completionNotify";
import { SettingsGroup, SettingsPage, SettingsRow } from "./SettingsLayout";
import SettingsSwitch from "./SettingsSwitch";

function permission(): NotificationPermission | "unsupported" {
  return typeof Notification === "undefined" ? "unsupported" : Notification.permission;
}

/** Both settings live in this window's localStorage. Conversation.tsx reads them when a turn ends. */
export default function NotificationsSettingsPage() {
  const [notify, setNotify] = useState(() => notifyPrefEnabled());
  const [sound, setSound] = useState(() => soundPrefEnabled());
  const [allowed, setAllowed] = useState(permission);

  const changeNotify = (next: boolean) => {
    setNotify(next);
    localStorage.setItem(NOTIFY_PREF_KEY, String(next));
    if (next && allowed === "default") {
      void Notification.requestPermission().then(setAllowed).catch(() => {});
    }
  };
  const changeSound = (next: boolean) => {
    setSound(next);
    localStorage.setItem(SOUND_PREF_KEY, String(next));
  };

  return (
    <SettingsPage title="Notifications" description="What Marionette does when a turn ends." testId="notifications-settings">
      <SettingsGroup>
        <SettingsRow
          testId="notifications-row-notify"
          label="Desktop notification"
          description="Shows a notice when a turn ends and Marionette is not in front."
          state={notify && allowed === "denied" ? "Blocked in system settings" : undefined}
          control={<SettingsSwitch on={notify} label="Desktop notification" onChange={changeNotify} testId="notifications-switch-notify" />}
        />
        <SettingsRow
          testId="notifications-row-sound"
          label="Sound"
          description="Plays a short tone when a turn ends."
          control={<SettingsSwitch on={sound} label="Sound" onChange={changeSound} testId="notifications-switch-sound" />}
        />
      </SettingsGroup>
    </SettingsPage>
  );
}
