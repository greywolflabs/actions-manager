export const generateBuildDetectionName = (
  technology: string,
  buildTypeName: string,
  repository = ''
): string => {
  const tech = technology.toLowerCase().replace('.js', '').replace('/', '_');
  const buildType = buildTypeName.toLowerCase();

  let name = `${tech}_${buildType}`;

  if (buildType.includes('maven') || buildType.includes('gradle')) {
    name += '_cicd';
  } else if (buildType.includes('npm') || buildType.includes('node')) {
    name += '_pipeline';
  } else {
    name += '_build';
  }

  if (repository) {
    const repoName = repository.split('/').pop()!.toLowerCase()
      .replace(/[^a-z0-9]/g, '_')
      .substring(0, 15);
    name = `${repoName}_${name}`;
  }

  return name;
};

export const generateTemplateName = (
  templateType: string,
  buildType = '',
  projectCode = ''
): string => {
  const tech = buildType ? buildType.toLowerCase() : '';
  const project = projectCode ? `${projectCode} ` : '';

  switch (templateType) {
    case 'standard':
      return tech
        ? `${project}${tech.charAt(0).toUpperCase() + tech.slice(1)} CI/CD Pipeline`
        : `${project}CI/CD Pipeline`;
    case 'reusable':
      return tech
        ? `${project}${tech.charAt(0).toUpperCase() + tech.slice(1)} Reusable Workflow`
        : `${project}Reusable Build Workflow`;
    case 'build':
      return tech
        ? `${project}${tech.charAt(0).toUpperCase() + tech.slice(1)} Build Process`
        : `${project}Build Process`;
    default:
      return `${project}Workflow`;
  }
};
