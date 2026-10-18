// Status management functions
export const getStatusIcon = (status: string): string => {
  switch (status.toLowerCase()) {
    case 'success': return '✅';
    case 'failure': return '❌';
    case 'in_progress': case 'queued': case 'pending': return '🟡';
    case 'cancelled': return '⚫';
    default: return '❓';
  }
};
