import { ref } from 'vue'
import apiClient, {
  clearStoredSession,
  getStoredToken,
  getStoredUser,
  storeSession
} from '@/services/api'
import type { AuthTokenResponse, User } from '@/types'

export const currentUser = ref<User | null>(getStoredUser())

export function isAuthenticated(): boolean {
  return Boolean(getStoredToken())
}

export async function register(username: string, password: string): Promise<User> {
  const response = await apiClient.post<User>('/api/auth/register', { username, password })
  return response.data
}

export async function login(username: string, password: string): Promise<User> {
  const response = await apiClient.post<AuthTokenResponse>('/api/auth/login', { username, password })
  storeSession(response.data.access_token, response.data.user)
  currentUser.value = response.data.user
  return response.data.user
}

export async function restoreCurrentUser(): Promise<User | null> {
  if (!getStoredToken()) {
    currentUser.value = null
    return null
  }
  const response = await apiClient.get<User>('/api/auth/me')
  currentUser.value = response.data
  return response.data
}

export function logout(): void {
  clearStoredSession()
  currentUser.value = null
}
