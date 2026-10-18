import { getStatusIcon } from './statusUtils';

describe('statusUtils', () => {
  describe('getStatusIcon', () => {
    test('should return success icon for success status', () => {
      expect(getStatusIcon('success')).toBe('✅');
      expect(getStatusIcon('SUCCESS')).toBe('✅');
    });

    test('should return failure icon for failure status', () => {
      expect(getStatusIcon('failure')).toBe('❌');
      expect(getStatusIcon('FAILURE')).toBe('❌');
    });

    test('should return warning icon for in_progress status', () => {
      expect(getStatusIcon('in_progress')).toBe('🟡');
      expect(getStatusIcon('IN_PROGRESS')).toBe('🟡');
    });

    test('should return warning icon for queued status', () => {
      expect(getStatusIcon('queued')).toBe('🟡');
      expect(getStatusIcon('QUEUED')).toBe('🟡');
    });

    test('should return warning icon for pending status', () => {
      expect(getStatusIcon('pending')).toBe('🟡');
      expect(getStatusIcon('PENDING')).toBe('🟡');
    });

    test('should return cancelled icon for cancelled status', () => {
      expect(getStatusIcon('cancelled')).toBe('⚫');
      expect(getStatusIcon('CANCELLED')).toBe('⚫');
    });

    test('should return unknown icon for unknown status', () => {
      expect(getStatusIcon('unknown')).toBe('❓');
      expect(getStatusIcon('invalid')).toBe('❓');
      expect(getStatusIcon('')).toBe('❓');
    });
  });
});
