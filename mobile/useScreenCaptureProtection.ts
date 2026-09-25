/**
 * useScreenCaptureProtection — for the NurseConnect MOBILE app (Expo / React Native).
 *
 * NOTE: the mobile repo was not part of the upload, so this is written against
 * the standard Expo APIs, not your exact code. Adjust imports/paths to match.
 *
 * What it does (these are REAL OS-level controls, unlike the web):
 *   - Android: sets FLAG_SECURE while the screen is focused → screenshots and
 *     screen recordings come out black, and the app is blanked in "Recents".
 *   - iOS: blocks screen *recording* / mirroring of the view (iOS cannot block
 *     screenshots), and reports screenshots so they can be audited.
 *   - Both: sends "screenshot_taken" to the backend audit log
 *     (POST /api/visits/{bookingId}/report/client-events).
 *
 * Install:   npx expo install expo-screen-capture
 * Android 14+ screenshot *detection* also needs, in app.json:
 *   "android": { "permissions": ["android.permission.DETECT_SCREEN_CAPTURE"] }
 *
 * Use it on EVERY screen that shows patient information (care summary,
 * visit report, prescriptions, vitals, patient profile, documents):
 *
 *   export default function VisitReportScreen({ route }) {
 *     useScreenCaptureProtection({ bookingId: route.params.bookingId });
 *     ...
 *   }
 */
import { useCallback } from "react";
import { Alert } from "react-native";
import { useFocusEffect } from "@react-navigation/native";
import * as ScreenCapture from "expo-screen-capture";

// Replace with your app's authenticated API helper (must send the Bearer token).
import { apiFetch } from "../lib/api";

type Options = {
  /** Booking the on-screen PHI belongs to; enables audit logging. */
  bookingId?: string;
  /** Screen name for the audit trail. Max 40 chars. */
  surface?: string;
  /** Show an alert after a screenshot (iOS / Android 14+). Default true. */
  warnOnScreenshot?: boolean;
};

export function useScreenCaptureProtection({
  bookingId,
  surface = "mobile_care_summary",
  warnOnScreenshot = true,
}: Options = {}) {
  useFocusEffect(
    useCallback(() => {
      // A unique key per screen so leaving one protected screen doesn't
      // un-protect another that is still mounted underneath.
      const key = `phi:${surface}:${bookingId ?? "none"}`;
      let active = true;

      ScreenCapture.preventScreenCaptureAsync(key).catch(() => {
        /* unsupported platform (e.g. web build) — nothing to do */
      });

      const sub = ScreenCapture.addScreenshotListener(() => {
        if (!active) return;
        if (bookingId) {
          apiFetch(`/api/visits/${bookingId}/report/client-events`, {
            method: "POST",
            body: JSON.stringify({ event: "screenshot_taken", surface: surface.slice(0, 40) }),
          }).catch(() => {});
        }
        if (warnOnScreenshot) {
          Alert.alert(
            "Patient information",
            "Screenshots of patient information are not permitted. This has been logged.",
          );
        }
      });

      return () => {
        active = false;
        sub.remove();
        ScreenCapture.allowScreenCaptureAsync(key).catch(() => {});
      };
    }, [bookingId, surface, warnOnScreenshot]),
  );
}
