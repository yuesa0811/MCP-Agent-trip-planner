import axios from 'axios'
import type {
  AdminTaskDetail,
  AdminTaskListResponse,
  TaskDetail,
  TaskListResponse,
  TaskStatus,
  TaskSubmitResponse,
  TripFormData,
  TripPlan,
  TripPlanResponse,
  User
} from '@/types'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'
const TOKEN_KEY = 'trip_planner_access_token'
const USER_KEY = 'trip_planner_user'

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 600000,
  headers: { 'Content-Type': 'application/json' }
})

export function getStoredToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function getStoredUser(): User | null {
  const raw = localStorage.getItem(USER_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as User
  } catch {
    clearStoredSession()
    return null
  }
}

export function storeSession(token: string, user: User): void {
  const previous = getStoredUser()
  if (!previous || previous.id !== user.id) {
    sessionStorage.removeItem('tripPlan')
  }
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

export function clearStoredSession(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
  sessionStorage.removeItem('tripPlan')
}

apiClient.interceptors.request.use((config) => {
  const token = getStoredToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

apiClient.interceptors.response.use(
  response => response,
  error => {
    const isLoginRequest = error.config?.url?.endsWith('/api/auth/login')
    if (error.response?.status === 401 && !isLoginRequest) {
      clearStoredSession()
      window.dispatchEvent(new Event('auth:unauthorized'))
    }
    return Promise.reject(error)
  }
)

export function apiErrorMessage(error: unknown, fallback: string): string {
  if (!axios.isAxiosError(error)) return error instanceof Error ? error.message : fallback
  const detail = error.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail.map(item => item?.msg).filter(Boolean).join('；') || fallback
  }
  return error.message || fallback
}

export async function generateTripPlan(formData: TripFormData): Promise<TripPlanResponse> {
  const response = await apiClient.post<TripPlanResponse>('/api/trip/plan', formData)
  return response.data
}

export async function submitTripTask(formData: TripFormData): Promise<TaskSubmitResponse> {
  const response = await apiClient.post<TaskSubmitResponse>('/api/trip/tasks', formData)
  return response.data
}

export async function getTripTask(taskId: string): Promise<TaskDetail> {
  const response = await apiClient.get<TaskDetail>(`/api/trip/tasks/${encodeURIComponent(taskId)}`)
  return response.data
}

export async function getTripTasks(options: {
  page?: number
  pageSize?: number
  status?: TaskStatus
} = {}): Promise<TaskListResponse> {
  const response = await apiClient.get<TaskListResponse>('/api/trip/tasks', {
    params: {
      page: options.page || 1,
      page_size: options.pageSize || 20,
      status: options.status
    }
  })
  return response.data
}

export async function saveTripTaskPlan(taskId: string, plan: TripPlan): Promise<TaskDetail> {
  const response = await apiClient.patch<TaskDetail>(
    `/api/trip/tasks/${encodeURIComponent(taskId)}/result`,
    plan
  )
  return response.data
}

export async function getAttractionPhoto(name: string): Promise<string | null> {
  const response = await apiClient.get<{ data: { photo_url: string | null } }>('/api/poi/photo', {
    params: { name }
  })
  return response.data.data.photo_url
}

export async function getAdminTasks(options: {
  page?: number
  pageSize?: number
  status?: TaskStatus
  username?: string
} = {}): Promise<AdminTaskListResponse> {
  const response = await apiClient.get<AdminTaskListResponse>('/api/admin/tasks', {
    params: {
      page: options.page || 1,
      page_size: options.pageSize || 20,
      status: options.status,
      username: options.username || undefined
    }
  })
  return response.data
}

export async function getAdminTask(taskId: string): Promise<AdminTaskDetail> {
  const response = await apiClient.get<AdminTaskDetail>(
    `/api/admin/tasks/${encodeURIComponent(taskId)}`
  )
  return response.data
}

export async function healthCheck(): Promise<unknown> {
  const response = await apiClient.get('/health')
  return response.data
}

export default apiClient
