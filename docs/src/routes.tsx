import { createBrowserRouter, Navigate } from "react-router-dom";

import { DocsLayout } from "@/layouts/docs";
import { RootLayout } from "@/layouts/root";
import Commands from "@/pages/docs/commands";
import Configuration from "@/pages/docs/configuration";
import Contributing from "@/pages/docs/contributing";
import HowItWorks from "@/pages/docs/how-it-works";
import Install from "@/pages/docs/install";
import Quickstart from "@/pages/docs/quickstart";
import Rules from "@/pages/docs/rules";
import SelfHosted from "@/pages/docs/self-hosted";
import StaticAnalysis from "@/pages/docs/static-analysis";
import Tokens from "@/pages/docs/tokens";
import Home from "@/pages/home";
import NotFound from "@/pages/not-found";

export const router = createBrowserRouter([
  {
    element: <RootLayout />,
    children: [
      { path: "/", element: <Home /> },
      {
        path: "/docs",
        element: <DocsLayout />,
        children: [
          { index: true, element: <Navigate to="/docs/install" replace /> },
          { path: "install", element: <Install /> },
          { path: "quickstart", element: <Quickstart /> },
          { path: "commands", element: <Commands /> },
          { path: "configuration", element: <Configuration /> },
          { path: "rules", element: <Rules /> },
          { path: "tokens", element: <Tokens /> },
          { path: "how-it-works", element: <HowItWorks /> },
          { path: "static-analysis", element: <StaticAnalysis /> },
          { path: "self-hosted", element: <SelfHosted /> },
          { path: "contributing", element: <Contributing /> },
        ],
      },
      { path: "*", element: <NotFound /> },
    ],
  },
]);
