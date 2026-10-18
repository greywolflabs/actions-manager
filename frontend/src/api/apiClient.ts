/**
 * Configured Axios instance for ActionsManager API requests.
 *
 * Sends the server-issued HttpOnly session cookie with API requests, and the
 * project page's id as X-Project-Id (see projectHint).
 */
import axios from "axios";
import config from "../config";
import { projectHintHeaders } from "./projectHint";

const apiClient = axios.create({
  baseURL: config.BACKEND_URL,
  withCredentials: true,
});

apiClient.interceptors.request.use((request) => {
  for (const [name, value] of Object.entries(projectHintHeaders())) {
    request.headers.set(name, value);
  }
  return request;
});

export default apiClient;
