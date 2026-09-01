/** Keep the public Scout surface dark unless the build explicitly enables it. */
export function isDarkScoutRoute(pathname: string, enabled: string | undefined): boolean {
  return (pathname === "/scout" || pathname.startsWith("/scout/")) && enabled !== "true";
}
