/**
 * Stands in for components/AppHeader in tests that mock react-router without
 * NavLink or Link, which the real header's tabs need. Use it with
 * vi.mock("./components/AppHeader", () => import("./test/appHeaderStub")).
 */
import React from "react";

export function AppHeader(): React.ReactElement {
  return <header data-testid="app-header" />;
}

export function AppPage({ children }: Readonly<{ children: React.ReactNode }>): React.ReactElement {
  return <main>{children}</main>;
}

export function AppShell({ children }: Readonly<{ children: React.ReactNode }>): React.ReactElement {
  return (
    <>
      <AppHeader />
      <AppPage>{children}</AppPage>
    </>
  );
}
