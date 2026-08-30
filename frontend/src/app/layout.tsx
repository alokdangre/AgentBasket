import type { Metadata } from "next";
import type { ReactNode } from "react";

import { ChatLauncher } from "@/components/chat-launcher";

import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "Ember & Leaf",
    template: "%s · Ember & Leaf",
  },
  description:
    "Café-made drinks, freshly roasted specialty coffee and remarkable tea from Bengaluru.",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en">
      <body>
        {children}
        <ChatLauncher />
      </body>
    </html>
  );
}
