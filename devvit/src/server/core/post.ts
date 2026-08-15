import { reddit } from '@devvit/web/server';

/** The single standing pinned post everyone verifies through -- not recreated per-user. */
export const createPost = async () => {
  return await reddit.submitCustomPost({
    title: 'Verify for Discord',
  });
};
