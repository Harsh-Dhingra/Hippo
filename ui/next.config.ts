import type { NextConfig } from "next";

const config: NextConfig = {
  // The browser never talks to the API directly and never holds a token: every
  // call goes through this server, which reads an httpOnly cookie. See lib/api.ts.
  reactStrictMode: true,
  output: "standalone",
};

export default config;
