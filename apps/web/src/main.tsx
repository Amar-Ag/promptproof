import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider, createBrowserRouter } from "react-router-dom";

import { App } from "./App";
import { ScanPage } from "./pages/ScanPage";
import { ScansPage } from "./pages/ScansPage";
import { TargetPage } from "./pages/TargetPage";
import { TargetsPage } from "./pages/TargetsPage";
import "./styles.css";

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, element: <TargetsPage /> },
      { path: "targets/:familyId", element: <TargetPage /> },
      { path: "scans", element: <ScansPage /> },
      { path: "scans/:scanId", element: <ScanPage /> },
    ],
  },
]);

const root = document.getElementById("root");
if (!root) throw new Error("#root is missing from index.html");

createRoot(root).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
